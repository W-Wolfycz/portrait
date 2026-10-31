"""结果落库：窗口、条目与游标的 SQLite 仓储（宿主自己的库，不碰 CM）。

三条硬规则（复用时不要破坏）：

1. **游标与结果同事务提交**：要么条目和游标一起生效，要么都不生效；
2. **幂等**：``window_id`` 与 ``item_id`` 都是内容派生的稳定 ID，重复处理同一窗口
   只会留下同一批行（``INSERT OR IGNORE``）；
3. **失败不推进游标**：:meth:`SqliteSink.record_failure` 只记录状态与错误，
   下次仍会重读同一窗口（至少一次语义）。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .contracts import (
    Cursor,
    ExtractionResult,
    SessionKey,
    dumps_json,
    sha256_hex,
    utc_now_iso,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
    session_key    TEXT PRIMARY KEY,
    platform_name  TEXT NOT NULL,
    message_type   TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    cursor_token   TEXT NOT NULL DEFAULT '',
    last_status    TEXT NOT NULL DEFAULT '',
    last_error     TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS window (
    window_id      TEXT PRIMARY KEY,
    session_key    TEXT NOT NULL,
    platform_name  TEXT NOT NULL,
    message_type   TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    start_token    TEXT NOT NULL,
    end_token      TEXT NOT NULL,
    unit_count     INTEGER NOT NULL DEFAULT 0,
    message_count  INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL,
    model          TEXT NOT NULL DEFAULT '',
    prompt_digest  TEXT NOT NULL DEFAULT '',
    usage_json     TEXT NOT NULL DEFAULT '{}',
    dropped_json   TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_window_session ON window(session_key, created_at);

CREATE TABLE IF NOT EXISTS item (
    item_id         TEXT PRIMARY KEY,
    window_id       TEXT NOT NULL,
    session_key     TEXT NOT NULL,
    item_index      INTEGER NOT NULL,
    subject_key     TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL,
    evidence_quote  TEXT NOT NULL DEFAULT '',
    importance      REAL NOT NULL DEFAULT 0.5,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    consumed_at     TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_item_pending ON item(created_at) WHERE consumed_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_item_subject ON item(subject_key, created_at);

CREATE TABLE IF NOT EXISTS runtime_stat (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class CommitOutcome:
    """一次提交的结果统计（用于日志与测试断言）。"""

    window_inserted: bool
    items_inserted: int
    items_total: int
    cursor_token: str

    @property
    def duplicate(self) -> bool:
        return not self.window_inserted


class SqliteSink:
    """宿主自己的结果库（WAL）。单线程使用；多线程请各自持有实例。"""

    def __init__(self, db_path: str, *, busy_timeout_ms: int = 5000) -> None:
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._connection = sqlite3.connect(db_path, timeout=busy_timeout_ms / 1000)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    # -- 游标 -----------------------------------------------------------

    def load_cursor(self, session: SessionKey) -> Cursor | None:
        row = self._connection.execute(
            "SELECT cursor_token FROM cursor WHERE session_key = ?",
            (session.key,),
        ).fetchone()
        if row is None:
            return None
        token = str(row["cursor_token"] or "")
        if not token:
            return None
        return Cursor.parse(token)

    def cursor_status(self, session: SessionKey) -> tuple[str, str]:
        row = self._connection.execute(
            "SELECT last_status, last_error FROM cursor WHERE session_key = ?",
            (session.key,),
        ).fetchone()
        if row is None:
            return ("", "")
        return (str(row["last_status"] or ""), str(row["last_error"] or ""))

    def ensure_baseline(self, session: SessionKey, cursor: Cursor) -> None:
        """新会话只建基线不重放历史：仅在无游标行时写入。"""
        now = utc_now_iso()
        self._connection.execute(
            "INSERT OR IGNORE INTO cursor "
            "(session_key, platform_name, message_type, session_id, cursor_token, "
            " last_status, last_error, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'baseline', '', ?)",
            (
                session.key,
                session.platform_name,
                session.message_type,
                session.session_id,
                cursor.token(),
                now,
            ),
        )
        self._connection.commit()

    def list_cursors(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT session_key, cursor_token, last_status, last_error, updated_at "
            "FROM cursor ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    # -- 提交 -----------------------------------------------------------

    def commit(
        self,
        result: ExtractionResult,
        next_cursor: Cursor,
        *,
        unit_count: int = 0,
    ) -> CommitOutcome:
        """幂等提交：窗口 + 条目 + 游标 在同一事务里生效。"""
        window = result.window
        now = utc_now_iso()
        cursor_obj = self._connection.cursor()
        try:
            cursor_obj.execute("BEGIN IMMEDIATE")
            inserted_window = cursor_obj.execute(
                "INSERT OR IGNORE INTO window "
                "(window_id, session_key, platform_name, message_type, session_id, "
                " start_token, end_token, unit_count, message_count, status, model, "
                " prompt_digest, usage_json, dropped_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    window.window_id,
                    window.session.key,
                    window.session.platform_name,
                    window.session.message_type,
                    window.session.session_id,
                    window.start.token(),
                    window.end.token(),
                    max(0, int(unit_count or 0)),
                    len(window.messages),
                    result.status,
                    result.model,
                    result.prompt_digest,
                    dumps_json(dict(result.usage or {})),
                    dumps_json(list(result.dropped or ())),
                    now,
                ),
            ).rowcount

            inserted_items = 0
            for item in result.items:
                item_id = sha256_hex(
                    window.window_id, item.index, item.subject_key, item.text
                )
                inserted_items += cursor_obj.execute(
                    "INSERT OR IGNORE INTO item "
                    "(item_id, window_id, session_key, item_index, subject_key, category, "
                    " text, evidence_quote, importance, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item_id,
                        window.window_id,
                        window.session.key,
                        item.index,
                        item.subject_key,
                        item.category,
                        item.text,
                        item.evidence_quote,
                        float(item.importance),
                        dumps_json(dict(item.payload or {})),
                        now,
                    ),
                ).rowcount

            cursor_obj.execute(
                "INSERT INTO cursor "
                "(session_key, platform_name, message_type, session_id, cursor_token, "
                " last_status, last_error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '', ?) "
                "ON CONFLICT(session_key) DO UPDATE SET "
                " cursor_token = excluded.cursor_token, "
                " last_status = excluded.last_status, "
                " last_error = '', "
                " updated_at = excluded.updated_at",
                (
                    window.session.key,
                    window.session.platform_name,
                    window.session.message_type,
                    window.session.session_id,
                    next_cursor.token(),
                    result.status,
                    now,
                ),
            )
            cursor_obj.execute("COMMIT")
        except BaseException:
            cursor_obj.execute("ROLLBACK")
            raise
        return CommitOutcome(
            window_inserted=bool(inserted_window),
            items_inserted=int(inserted_items),
            items_total=len(result.items),
            cursor_token=next_cursor.token(),
        )

    def record_failure(self, session: SessionKey, status: str, error: str) -> None:
        """记录失败状态但不推进游标（无游标行时建一条空游标行）。"""
        now = utc_now_iso()
        self._connection.execute(
            "INSERT INTO cursor "
            "(session_key, platform_name, message_type, session_id, cursor_token, "
            " last_status, last_error, updated_at) "
            "VALUES (?, ?, ?, ?, '', ?, ?, ?) "
            "ON CONFLICT(session_key) DO UPDATE SET "
            " last_status = excluded.last_status, "
            " last_error = excluded.last_error, "
            " updated_at = excluded.updated_at",
            (
                session.key,
                session.platform_name,
                session.message_type,
                session.session_id,
                str(status or "")[:40],
                str(error or "")[:400],
                now,
            ),
        )
        self._connection.commit()

    def record_status(self, session: SessionKey, status: str) -> None:
        """仅更新状态文本（例如 ``below_threshold``），不改游标。"""
        now = utc_now_iso()
        self._connection.execute(
            "UPDATE cursor SET last_status = ?, last_error = '', updated_at = ? "
            "WHERE session_key = ?",
            (str(status or "")[:40], now, session.key),
        )
        self._connection.commit()

    # -- 条目查询 -------------------------------------------------------

    def count_items(self, *, consumed: bool | None = None) -> int:
        if consumed is None:
            sql = "SELECT COUNT(*) AS n FROM item"
            params: list[Any] = []
        elif consumed:
            sql = "SELECT COUNT(*) AS n FROM item WHERE consumed_at IS NOT NULL"
            params = []
        else:
            sql = "SELECT COUNT(*) AS n FROM item WHERE consumed_at IS NULL"
            params = []
        row = self._connection.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def list_items(
        self,
        *,
        session: SessionKey | None = None,
        consumed: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if session is not None:
            clauses.append("session_key = ?")
            params.append(session.key)
        if consumed is True:
            clauses.append("consumed_at IS NOT NULL")
        elif consumed is False:
            clauses.append("consumed_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM item {where} ORDER BY created_at ASC, item_index ASC "
            f"LIMIT ?",
            [*params, max(1, int(limit))],
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_items_consumed(self, item_ids: Sequence[str]) -> int:
        """把条目标记为已消费（供宿主的合并/汇总流程调用）。"""
        ids = [str(item) for item in item_ids if str(item).strip()]
        if not ids:
            return 0
        now = utc_now_iso()
        changed = 0
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            changed += self._connection.execute(
                f"UPDATE item SET consumed_at = ? "
                f"WHERE item_id IN ({placeholders}) AND consumed_at IS NULL",
                [now, *chunk],
            ).rowcount
        self._connection.commit()
        return int(changed)

    # -- 运行统计（预算等） ---------------------------------------------

    def get_stat(self, key: str, default: str = "") -> str:
        row = self._connection.execute(
            "SELECT v FROM runtime_stat WHERE k = ?", (str(key),)
        ).fetchone()
        return str(row["v"]) if row else default

    def set_stat(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO runtime_stat (k, v, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at",
            (str(key), str(value), utc_now_iso()),
        )
        self._connection.commit()

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:  # pragma: no cover
            pass

__all__ = ["CommitOutcome", "SqliteSink"]
