"""CM 消息源：只读两种方言的 CM 库，输出统一的 :class:`Window`。

方言一：影子表（``chat_memory_messages`` + ``chat_memory_message_bots``）
    - 一条物理消息一行（跨 BOT 实例去重）；参与行承载每实例状态。
    - 游标 = ``seq``（会话内递增，跨实例一致）。

方言二：旧表（``chat_memory_records``）
    - 每个实例各存一份；游标 = ``(created_at, id)`` 严格复合。
    - 一个 UMO 内的消息即一个会话；同群多实例会看到重复消息（调用方自行处理）。

约定：

- 连接一律 ``mode=ro``，本模块不会写 CM。
- 撤回（``recalled_at``）与软删（``deleted_at``）默认过滤掉。
- 内容类型白名单在 Python 侧复核（``content_kind`` 是 JSON 文本）。
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterable, Sequence

from .contracts import (
    DIALECT_AUTO,
    DIALECT_LEGACY,
    DIALECT_SHADOW,
    CmMessage,
    CmSource,
    Cursor,
    Participant,
    SessionKey,
    Window,
    parse_kind_list,
)

#: 影子表自身 schema 版本所在表。
_SHADOW_META_TABLE = "chat_memory_shadow_meta"
_SHADOW_MESSAGES_TABLE = "chat_memory_messages"
_SHADOW_BOTS_TABLE = "chat_memory_message_bots"
_LEGACY_TABLE = "chat_memory_records"


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """以只读方式打开 SQLite；路径不存在直接报错（不隐式建库）。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"CM 库不存在: {db_path}")
    uri = f"file:{db_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """读取表的列名集合；表不存在返回空集。"""
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _row_get(row: sqlite3.Row, column: str, default: Any = None) -> Any:
    """安全读取列：老版本 / 精简 schema 缺少可选列时不抛 IndexError。"""
    try:
        keys = row.keys()
    except AttributeError:  # pragma: no cover - 非 sqlite3.Row
        return default
    if column not in keys:
        return default
    value = row[column]
    return default if value is None else value


def _chunked(values: Sequence[str], size: int = 500) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


class ShadowCmSource:
    """影子表方言（物理去重消息 + 参与行）。"""

    dialect = DIALECT_SHADOW

    def __init__(
        self,
        db_path: str,
        *,
        include_kinds: Sequence[str] = ("text",),
        exclude_recalled: bool = True,
        exclude_deleted: bool = True,
        assume_kinds_when_missing: Sequence[str] = ("text",),
    ) -> None:
        self.db_path = db_path
        self.include_kinds = tuple(include_kinds or ())
        # CM 正常会写 content_kind（默认 text）；老库/精简 schema 缺列时按 text 处理，
        # 避免「白名单开着但所有消息都被静默丢掉」。
        self.assume_kinds = tuple(assume_kinds_when_missing or ())
        self.exclude_recalled = exclude_recalled
        self.exclude_deleted = exclude_deleted
        self._connection = _connect_ro(db_path)
        columns = _table_columns(self._connection, _SHADOW_MESSAGES_TABLE)
        self._has_recalled = "recalled_at" in columns
        self._has_deleted = "deleted_at" in columns

    # -- 元信息 ---------------------------------------------------------

    @property
    def schema_version(self) -> int | None:
        """影子表 schema 版本；版本不等意味着表刚被重建（数据可能为空）。"""
        try:
            row = self._connection.execute(
                f"SELECT value FROM {_SHADOW_META_TABLE} WHERE key = ?",
                ("schema_version",),
            ).fetchone()
        except sqlite3.Error:
            return None
        try:
            return int(row[0]) if row else None
        except (TypeError, ValueError):
            return None

    # -- 会话 -----------------------------------------------------------

    def list_sessions(self) -> list[SessionKey]:
        rows = self._connection.execute(
            f"SELECT DISTINCT platform_name, message_type, session_id "
            f"FROM {_SHADOW_MESSAGES_TABLE} "
            f"WHERE platform_name <> '' AND session_id <> ''"
        ).fetchall()
        sessions = [
            SessionKey(str(row[0]), str(row[1]), str(row[2])) for row in rows
        ]
        sessions.sort(key=lambda item: item.key)
        return sessions

    def latest_cursor(self, session: SessionKey) -> Cursor | None:
        row = self._connection.execute(
            f"SELECT MAX(seq) FROM {_SHADOW_MESSAGES_TABLE} "
            f"WHERE platform_name = ? AND message_type = ? AND session_id = ?",
            (session.platform_name, session.message_type, session.session_id),
        ).fetchone()
        if not row or row[0] is None:
            return None
        return Cursor(seq=int(row[0]))

    # -- 窗口 -----------------------------------------------------------

    def read_window(
        self, session: SessionKey, after: Cursor | None, limit: int
    ) -> Window | None:
        after_seq = -1 if after is None or after.seq is None else int(after.seq)
        sql = [
            f"SELECT * FROM {_SHADOW_MESSAGES_TABLE}",
            "WHERE platform_name = ? AND message_type = ? AND session_id = ?",
            "AND seq > ?",
        ]
        params: list[Any] = [
            session.platform_name,
            session.message_type,
            session.session_id,
            after_seq,
        ]
        if self.exclude_deleted and self._has_deleted:
            sql.append("AND deleted_at IS NULL")
        if self.exclude_recalled and self._has_recalled:
            sql.append("AND recalled_at IS NULL")
        sql.append("ORDER BY seq ASC LIMIT ?")
        params.append(max(1, int(limit)))

        rows = self._connection.execute(" ".join(sql), params).fetchall()
        messages = self._build_messages(session, rows, after=after)
        if not messages:
            return None
        start = after if after is not None else Cursor(seq=int(messages[0].seq) - 1)
        end = Cursor(seq=int(messages[-1].seq))
        return Window(session=session, start=start, end=end, messages=messages)

    def count_messages_after(self, session: SessionKey, after: Cursor | None) -> int:
        """游标之后还有多少条**可读**消息（撤回/软删已排除）。

        只做 COUNT，不解析正文与内容类型，因此可以每 tick 廉价调用；
        它用于自适应 ticker 判断积压，不参与精确阈值判定。
        """
        after_seq = -1 if after is None or after.seq is None else int(after.seq)
        sql = [
            f"SELECT COUNT(*) FROM {_SHADOW_MESSAGES_TABLE}",
            "WHERE platform_name = ? AND message_type = ? AND session_id = ?",
            "AND seq > ?",
        ]
        params: list[Any] = [
            session.platform_name,
            session.message_type,
            session.session_id,
            after_seq,
        ]
        if self.exclude_deleted and self._has_deleted:
            sql.append("AND deleted_at IS NULL")
        if self.exclude_recalled and self._has_recalled:
            sql.append("AND recalled_at IS NULL")
        row = self._connection.execute(" ".join(sql), params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _build_messages(
        self,
        session: SessionKey,
        rows: Sequence[sqlite3.Row],
        *,
        after: Cursor | None,
    ) -> tuple[CmMessage, ...]:
        if not rows:
            return ()
        uids = [str(row["msg_uid"]) for row in rows]
        participants = self._load_participants(uids)
        messages: list[CmMessage] = []
        for row in rows:
            kinds = parse_kind_list(_row_get(row, "content_kind")) or self.assume_kinds
            if self.include_kinds and not set(kinds) & set(self.include_kinds):
                continue
            uid = str(_row_get(row, "msg_uid", ""))
            messages.append(
                CmMessage(
                    msg_uid=uid,
                    session=session,
                    seq=int(_row_get(row, "seq", 0) or 0),
                    sender_id=str(_row_get(row, "sender_id", "") or ""),
                    sender_nickname=str(_row_get(row, "sender_nickname", "") or ""),
                    content=str(_row_get(row, "content", "") or ""),
                    content_kinds=kinds,
                    platform_timestamp=int(_row_get(row, "platform_timestamp", 0) or 0),
                    recalled=_row_get(row, "recalled_at") is not None,
                    deleted=_row_get(row, "deleted_at") is not None,
                    participants=participants.get(uid, ()),
                    raw=dict(row),
                )
            )
        return tuple(messages)

    def _load_participants(
        self, msg_uids: Sequence[str]
    ) -> dict[str, tuple[Participant, ...]]:
        collected: dict[str, list[Participant]] = {}
        for chunk in _chunked(list(msg_uids)):
            placeholders = ",".join("?" for _ in chunk)
            rows = self._connection.execute(
                f"SELECT * FROM {_SHADOW_BOTS_TABLE} "
                f"WHERE msg_uid IN ({placeholders}) ORDER BY id ASC",
                list(chunk),
            ).fetchall()
            for row in rows:
                collected.setdefault(str(_row_get(row, "msg_uid", "")), []).append(
                    Participant(
                        platform_id=str(_row_get(row, "platform_id", "") or ""),
                        self_id=str(_row_get(row, "self_id", "") or ""),
                        conversation_id=str(_row_get(row, "conversation_id", "") or ""),
                        umo=str(_row_get(row, "umo", "") or ""),
                        role=str(_row_get(row, "role", "") or ""),
                        persona_id=str(_row_get(row, "persona_id", "") or ""),
                        turn_id=str(_row_get(row, "turn_id", "") or ""),
                        llm_status=str(_row_get(row, "llm_status", "") or ""),
                        target_id=str(_row_get(row, "target_id", "") or ""),
                    )
                )
        return {uid: tuple(items) for uid, items in collected.items()}

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:  # pragma: no cover - 关闭失败无需处理
            pass


class LegacyCmSource:
    """旧表方言（每实例各存一份）。"""

    dialect = DIALECT_LEGACY

    def __init__(
        self,
        db_path: str,
        *,
        include_kinds: Sequence[str] = ("text",),
        exclude_recalled: bool = True,
        exclude_deleted: bool = True,
        assume_kinds_when_missing: Sequence[str] = ("text",),
    ) -> None:
        self.db_path = db_path
        self.include_kinds = tuple(include_kinds or ())
        # CM 正常会写 content_kind（默认 text）；老库/精简 schema 缺列时按 text 处理，
        # 避免「白名单开着但所有消息都被静默丢掉」。
        self.assume_kinds = tuple(assume_kinds_when_missing or ())
        self.exclude_recalled = exclude_recalled
        self.exclude_deleted = exclude_deleted
        self._connection = _connect_ro(db_path)
        self._columns = _table_columns(self._connection, _LEGACY_TABLE)
        self._has_recalled = "recalled_at" in self._columns
        self._has_deleted = "deleted_at" in self._columns

    # -- 会话 -----------------------------------------------------------

    def _visibility_clause(self) -> str:
        """撤回/软删过滤；老库缺列时自动跳过对应过滤。"""
        parts: list[str] = []
        if self.exclude_deleted and self._has_deleted:
            parts.append("deleted_at IS NULL")
        if self.exclude_recalled and self._has_recalled:
            parts.append("recalled_at IS NULL")
        return "".join(f" AND {item}" for item in parts)

    def list_sessions(self) -> list[SessionKey]:
        wanted = (
            "platform_name",
            "message_type",
            "session_id",
            "umo",
            "group_id",
            "user_id",
        )
        selected = [name for name in wanted if name in self._columns]
        if not selected:
            return []
        rows = self._connection.execute(
            f"SELECT {', '.join(selected)} FROM {_LEGACY_TABLE}"
        ).fetchall()
        sessions: dict[str, SessionKey] = {}
        for row in rows:
            session = _legacy_session(row)
            if session is not None:
                sessions[session.key] = session
        return sorted(sessions.values(), key=lambda item: item.key)

    def _session_filter(self, session: SessionKey) -> tuple[str, list[Any]]:
        """按列可用性构建会话过滤条件；没有可用列时匹配不到任何行。"""
        branches: list[str] = []
        params: list[Any] = []
        columns = self._columns
        has_platform = "platform_name" in columns
        has_type = "message_type" in columns
        if has_platform and has_type and (
            "session_id" in columns or "group_id" in columns
        ):
            branch = "("
            if "session_id" in columns and "group_id" in columns:
                branch += (
                    "COALESCE(platform_name, '') = ? AND COALESCE(message_type, '') = ? "
                    "AND (COALESCE(session_id, '') = ? "
                    "     OR (COALESCE(session_id, '') = '' "
                    "         AND COALESCE(group_id, '') = ?))"
                )
                params.extend(
                    [
                        session.platform_name,
                        session.message_type,
                        session.session_id,
                        session.session_id,
                    ]
                )
            elif "session_id" in columns:
                branch += (
                    "COALESCE(platform_name, '') = ? AND COALESCE(message_type, '') = ? "
                    "AND COALESCE(session_id, '') = ?"
                )
                params.extend(
                    [session.platform_name, session.message_type, session.session_id]
                )
            else:
                branch += (
                    "COALESCE(platform_name, '') = ? AND COALESCE(message_type, '') = ? "
                    "AND COALESCE(group_id, '') = ?"
                )
                params.extend(
                    [session.platform_name, session.message_type, session.session_id]
                )
            branches.append(branch + ")")
        if "umo" in columns:
            branches.append("COALESCE(umo, '') = ?")
            params.append(
                f"{session.platform_name}:{session.message_type}:{session.session_id}"
            )
        if not branches:
            return ("WHERE 0", [])
        return ("WHERE (" + " OR ".join(branches) + ")", params)

    def latest_cursor(self, session: SessionKey) -> Cursor | None:
        where, params = self._session_filter(session)
        row = self._connection.execute(
            f"SELECT created_at, id FROM {_LEGACY_TABLE} {where}"
            f"{self._visibility_clause()} "
            f"ORDER BY created_at DESC, id DESC LIMIT 1",
            params,
        ).fetchone()
        if not row:
            return None
        return Cursor(created_at=str(row[0]), record_id=int(row[1]))

    # -- 窗口 -----------------------------------------------------------

    def count_messages_after(self, session: SessionKey, after: Cursor | None) -> int:
        """游标之后还有多少条可读消息（旧表方言）。"""
        where, params = self._session_filter(session)
        clauses = [where, self._visibility_clause()]
        if after is not None and after.created_at is not None:
            clauses.append("AND (created_at > ? OR (created_at = ? AND id > ?))")
            params = [
                *params,
                after.created_at,
                after.created_at,
                int(after.record_id or 0),
            ]
        sql = f"SELECT COUNT(*) FROM {_LEGACY_TABLE} {' '.join(clauses)}"
        row = self._connection.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def read_window(
        self, session: SessionKey, after: Cursor | None, limit: int
    ) -> Window | None:
        where, params = self._session_filter(session)
        clauses = [where]
        visibility = self._visibility_clause()
        if visibility:
            clauses.append(visibility)
        if after is not None and after.created_at is not None:
            clauses.append("AND (created_at > ? OR (created_at = ? AND id > ?))")
            params = [*params, after.created_at, after.created_at, int(after.record_id or 0)]
        sql = (
            f"SELECT * FROM {_LEGACY_TABLE} {' '.join(clauses)} "
            f"ORDER BY created_at ASC, id ASC LIMIT ?"
        )
        rows = self._connection.execute(sql, [*params, max(1, int(limit))]).fetchall()
        messages: list[CmMessage] = []
        for row in rows:
            kinds = parse_kind_list(_row_get(row, "content_kind")) or self.assume_kinds
            if self.include_kinds and not set(kinds) & set(self.include_kinds):
                continue
            row_session = _legacy_session(row)
            if row_session is None:
                continue
            row_id = int(_row_get(row, "id", 0) or 0)
            role = str(_row_get(row, "role", "") or "")
            sender_id = (
                _row_get(row, "self_id")
                if role == "assistant"
                else _row_get(row, "user_id")
            )
            messages.append(
                CmMessage(
                    msg_uid=f"legacy:{row_id}",
                    session=row_session,
                    seq=row_id,
                    sender_id=str(sender_id or ""),
                    sender_nickname=str(_row_get(row, "sender_nickname", "") or ""),
                    content=str(_row_get(row, "content", "") or ""),
                    content_kinds=kinds,
                    platform_timestamp=int(_row_get(row, "raw_timestamp", 0) or 0),
                    recalled=_row_get(row, "recalled_at") is not None,
                    deleted=_row_get(row, "deleted_at") is not None,
                    participants=(),
                    raw=dict(row),
                )
            )
        if not messages:
            return None
        first_token = Cursor(
            created_at=str(_row_get(rows[0], "created_at", "") or ""),
            record_id=int(_row_get(rows[0], "id", 0) or 0),
        )
        start = after if after is not None else first_token
        last = messages[-1]
        end = Cursor(
            created_at=str(last.raw.get("created_at")), record_id=int(last.seq)
        )
        return Window(session=session, start=start, end=end, messages=tuple(messages))

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:  # pragma: no cover
            pass


def _legacy_session(row: sqlite3.Row) -> SessionKey | None:
    """从旧表行推导物理会话，优先级：显式列 → ``umo`` → ``group_id``/``user_id``。"""
    columns = set(row.keys())
    platform = str(row["platform_name"] or "") if "platform_name" in columns else ""
    message_type = str(row["message_type"] or "") if "message_type" in columns else ""
    session_id = str(row["session_id"] or "") if "session_id" in columns else ""
    umo = str(row["umo"] or "") if "umo" in columns else ""
    if umo:
        parts = umo.split(":", 2)
        if len(parts) == 3:
            platform = platform or parts[0]
            message_type = message_type or parts[1]
            if not session_id:
                session_id = parts[2]
    if not session_id:
        group_id = str(row["group_id"] or "") if "group_id" in columns else ""
        user_id = str(row["user_id"] or "") if "user_id" in columns else ""
        session_id = group_id or user_id
    if not (platform and message_type and session_id):
        return None
    return SessionKey(platform, message_type, session_id)


def detect_dialect(db_path: str) -> str:
    """探测 CM 库形态：有影子表返回 ``shadow``，否则 ``legacy``。"""
    connection = _connect_ro(db_path)
    try:
        names = _table_names(connection)
    finally:
        connection.close()
    if _SHADOW_MESSAGES_TABLE in names:
        return DIALECT_SHADOW
    if _LEGACY_TABLE in names:
        return DIALECT_LEGACY
    raise RuntimeError(
        f"无法识别的 CM 库：既没有 {_SHADOW_MESSAGES_TABLE} 也没有 {_LEGACY_TABLE}"
    )


def open_cm_source(
    db_path: str,
    *,
    dialect: str = DIALECT_AUTO,
    include_kinds: Sequence[str] = ("text",),
    exclude_recalled: bool = True,
    exclude_deleted: bool = True,
    assume_kinds_when_missing: Sequence[str] = ("text",),
) -> CmSource:
    """打开只读 CM 源；``dialect="auto"`` 时按表存在性探测。"""
    chosen = dialect
    if chosen == DIALECT_AUTO:
        chosen = detect_dialect(db_path)
    if chosen == DIALECT_SHADOW:
        return ShadowCmSource(
            db_path,
            include_kinds=include_kinds,
            exclude_recalled=exclude_recalled,
            exclude_deleted=exclude_deleted,
            assume_kinds_when_missing=assume_kinds_when_missing,
        )
    if chosen == DIALECT_LEGACY:
        return LegacyCmSource(
            db_path,
            include_kinds=include_kinds,
            exclude_recalled=exclude_recalled,
            exclude_deleted=exclude_deleted,
            assume_kinds_when_missing=assume_kinds_when_missing,
        )
    raise ValueError(f"未知方言: {dialect!r}")


__all__ = [
    "CmSource",
    "LegacyCmSource",
    "ShadowCmSource",
    "detect_dialect",
    "open_cm_source",
]
