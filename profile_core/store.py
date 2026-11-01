"""画像库（profile.db）：游标、片段摘要、重置守卫。

与 CM 的 ``chat_memory.db`` 完全分开；本模块只写自己的库。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from .contracts import (
    Cursor,
    RawMessage,
    ResetDecision,
    SessionKey,
    sha256_hex,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
  session_key    TEXT PRIMARY KEY,
  platform_name  TEXT NOT NULL,
  message_type   TEXT NOT NULL,
  session_id     TEXT NOT NULL,
  seq            INTEGER NOT NULL DEFAULT 0,
  msg_uid        TEXT NOT NULL DEFAULT '',
  generation     INTEGER NOT NULL DEFAULT 0,
  schema_ver     INTEGER NOT NULL DEFAULT 0,
  dual_started   TEXT NOT NULL DEFAULT '',
  last_status    TEXT NOT NULL DEFAULT '',
  last_error     TEXT NOT NULL DEFAULT '',
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segment (
  segment_id      TEXT PRIMARY KEY,
  session_key    TEXT NOT NULL,
  start_seq      INTEGER NOT NULL,
  end_seq        INTEGER NOT NULL,
  message_count  INTEGER NOT NULL,
  status         TEXT NOT NULL,
  summary_json   TEXT NOT NULL,
  model          TEXT NOT NULL DEFAULT '',
  prompt_digest  TEXT NOT NULL DEFAULT '',
  usage_json     TEXT NOT NULL DEFAULT '{}',
  created_at     TEXT NOT NULL,
  consumed_at    TEXT,
  expires_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_segment_pending
  ON segment (session_key, consumed_at, created_at);
CREATE INDEX IF NOT EXISTS ix_segment_expiry
  ON segment (expires_at);

CREATE TABLE IF NOT EXISTS runtime_stat (
  k          TEXT PRIMARY KEY,
  v          TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_session (
  session_key   TEXT PRIMARY KEY,
  payload_json  TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_person (
  session_key   TEXT NOT NULL,
  sender_id     TEXT NOT NULL,
  display_name  TEXT NOT NULL DEFAULT '',
  payload_json  TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (session_key, sender_id)
);

CREATE INDEX IF NOT EXISTS ix_profile_person_session
  ON profile_person (session_key);

CREATE TABLE IF NOT EXISTS merge_run (
  run_id        TEXT PRIMARY KEY,
  run_date      TEXT NOT NULL,
  session_key   TEXT NOT NULL,
  segment_count  INTEGER NOT NULL DEFAULT 0,
  person_count  INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL,
  llm_status    TEXT NOT NULL DEFAULT '',
  error         TEXT NOT NULL DEFAULT '',
  started_at    TEXT NOT NULL,
  finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS ix_merge_run_session
  ON merge_run (session_key, run_date);
"""


@dataclass(frozen=True)
class StoredSegment:
    segment_id: str
    session_key: str
    start_seq: int
    end_seq: int
    message_count: int
    status: str
    summary_json: str
    created_at: str
    consumed_at: str
    expires_at: str


def utc_now_iso(moment: datetime | None = None) -> str:
    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text or "")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def detect_reset(
    cursor: Cursor | None,
    max_seq: int,
    msg_uid_at_cursor: str | None,
    shadow_meta: dict[str, str],
) -> ResetDecision:
    """判定游标是否应重置。

    - ``max_seq < cursor.seq``：会话被删空/重建；
    - 游标 seq 上的 uid 与记录不一致：位置漂移；
    - ``shadow_meta`` 的 schema/起始时间变化：影子表重建。
    """
    if cursor is None:
        return ResetDecision(False, "new")
    if int(max_seq) < int(cursor.seq):
        return ResetDecision(True, "seq_regressed")
    if (
        cursor.msg_uid
        and msg_uid_at_cursor
        and str(msg_uid_at_cursor) != cursor.msg_uid
    ):
        return ResetDecision(True, "uid_mismatch")
    schema_ver = _safe_int(shadow_meta.get("schema_version"), 0)
    if schema_ver and schema_ver != cursor.schema_ver:
        return ResetDecision(True, "schema_changed")
    dual_started = str(shadow_meta.get("dual_write_started_at") or "").strip()
    if dual_started and dual_started != cursor.dual_started:
        return ResetDecision(True, "schema_changed")
    return ResetDecision(False, "ok")


class ProfileStore:
    """profile.db 的读写封装；调用方负责 ``close()``。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._connection = sqlite3.connect(db_path, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    # -- 游标 -----------------------------------------------------------

    def get_cursor(self, session: SessionKey) -> Cursor | None:
        row = self._connection.execute(
            "SELECT * FROM cursor WHERE session_key = ?", (session.key,)
        ).fetchone()
        return self._to_cursor(row) if row else None

    def ensure_cursor(
        self,
        session: SessionKey,
        *,
        schema_ver: int = 0,
        dual_started: str = "",
        initial_seq: int = 0,
        initial_uid: str = "",
        moment: datetime | None = None,
    ) -> Cursor:
        current = self.get_cursor(session)
        if current is not None:
            return current
        now = utc_now_iso(moment)
        self._connection.execute(
            "INSERT OR IGNORE INTO cursor "
            "(session_key, platform_name, message_type, session_id, seq, msg_uid, "
            " generation, schema_ver, dual_started, last_status, last_error, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, '', '', ?)",
            (
                session.key,
                session.platform_name,
                session.message_type,
                session.session_id,
                max(0, int(initial_seq)),
                str(initial_uid),
                int(schema_ver),
                str(dual_started),
                now,
            ),
        )
        self._connection.commit()
        return self.get_cursor(session) or Cursor(
            session_key=session.key,
            seq=max(0, int(initial_seq)),
            msg_uid=str(initial_uid),
            generation=0,
            schema_ver=int(schema_ver),
            dual_started=str(dual_started),
        )

    def reset_cursor(
        self,
        session: SessionKey,
        *,
        reason: str,
        schema_ver: int | None = None,
        dual_started: str | None = None,
        moment: datetime | None = None,
    ) -> Cursor:
        current = self.ensure_cursor(session)
        now = utc_now_iso(moment)
        new_schema = current.schema_ver if schema_ver is None else int(schema_ver)
        new_dual = (
            current.dual_started if dual_started is None else str(dual_started)
        )
        self._connection.execute(
            "UPDATE cursor SET seq = 0, msg_uid = '', generation = generation + 1, "
            "schema_ver = ?, dual_started = ?, last_status = ?, last_error = '', "
            "updated_at = ? WHERE session_key = ?",
            (
                new_schema,
                new_dual,
                reason,
                now,
                session.key,
            ),
        )
        self._connection.commit()
        return self.get_cursor(session) or current

    def advance_cursor(
        self,
        session: SessionKey,
        *,
        seq: int,
        msg_uid: str,
        reason: str = "policy_advance",
        moment: datetime | None = None,
    ) -> Cursor:
        """政策拒绝时跳过若干消息：只推进游标，不写片段。"""
        current = self.ensure_cursor(session)
        target_seq = max(int(current.seq), int(seq))
        self._connection.execute(
            "UPDATE cursor SET seq = ?, msg_uid = ?, last_status = ?, "
            "last_error = '', updated_at = ? WHERE session_key = ?",
            (
                target_seq,
                str(msg_uid),
                str(reason),
                utc_now_iso(moment),
                session.key,
            ),
        )
        self._connection.commit()
        return self.get_cursor(session) or current

    def apply_schema_guard(
        self,
        shadow_meta: dict[str, str],
        *,
        moment: datetime | None = None,
    ) -> int:
        """影子表 schema/起始时间变化时，把所有会话代际 +1。"""
        schema_ver = _safe_int(shadow_meta.get("schema_version"), 0)
        dual_started = str(shadow_meta.get("dual_write_started_at") or "").strip()
        if not schema_ver and not dual_started:
            return 0
        now = utc_now_iso(moment)
        cursor = self._connection.execute(
            "UPDATE cursor SET seq = 0, msg_uid = '', generation = generation + 1, "
            "schema_ver = ?, dual_started = ?, last_status = 'schema_changed', "
            "last_error = '', updated_at = ? "
            "WHERE schema_ver <> ? OR dual_started <> ?",
            (schema_ver, dual_started, now, schema_ver, dual_started),
        )
        self._connection.commit()
        return int(cursor.rowcount or 0)

    # -- 片段摘要 -------------------------------------------------------

    def commit_segment(
        self,
        session: SessionKey,
        cursor: Cursor,
        messages: Sequence[RawMessage],
        summary_json: str | dict[str, Any],
        *,
        status: str = "success",
        model: str = "",
        prompt_digest: str = "",
        usage: dict[str, Any] | None = None,
        cursor_seq: int,
        cursor_msg_uid: str,
        ttl_days: int = 7,
        moment: datetime | None = None,
    ) -> str:
        """写入片段摘要并推进游标；两者同一事务。

        空片段直接抛 ``ValueError``，不写任何东西。
        """
        if not messages:
            raise ValueError("commit_segment: messages 不能为空")
        if cursor.session_key != session.key:
            raise ValueError("commit_segment: cursor 与 session 不匹配")
        now_dt = moment or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        created_at = utc_now_iso(now_dt)
        if int(ttl_days) <= 0:
            expires_at = "9999-12-31T23:59:59Z"
        else:
            expires_at = utc_now_iso(
                now_dt + timedelta(days=int(ttl_days))
            )
        segment_id = sha256_hex(
            session.key,
            cursor.generation,
            messages[0].msg_uid,
            messages[-1].msg_uid,
            len(messages),
        )
        payload = (
            summary_json
            if isinstance(summary_json, str)
            else json.dumps(summary_json, ensure_ascii=False)
        )
        usage_json = json.dumps(usage or {}, ensure_ascii=False)
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO segment "
                "(segment_id, session_key, start_seq, end_seq, message_count, status, "
                " summary_json, model, prompt_digest, usage_json, created_at, "
                " consumed_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    segment_id,
                    session.key,
                    int(messages[0].seq),
                    int(messages[-1].seq),
                    len(messages),
                    status,
                    payload,
                    model,
                    prompt_digest,
                    usage_json,
                    created_at,
                    expires_at,
                ),
            )
            updated = self._connection.execute(
                "UPDATE cursor SET seq = ?, msg_uid = ?, last_status = 'ok', "
                "last_error = '', updated_at = ? WHERE session_key = ?",
                (
                    int(cursor_seq),
                    str(cursor_msg_uid),
                    created_at,
                    session.key,
                ),
            )
            if updated.rowcount == 0:
                self._connection.execute(
                    "INSERT INTO cursor "
                    "(session_key, platform_name, message_type, session_id, seq, "
                    " msg_uid, generation, schema_ver, dual_started, last_status, "
                    " last_error, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', '', ?)",
                    (
                        session.key,
                        session.platform_name,
                        session.message_type,
                        session.session_id,
                        int(cursor_seq),
                        str(cursor_msg_uid),
                        cursor.generation,
                        cursor.schema_ver,
                        cursor.dual_started,
                        created_at,
                    ),
                )
        return segment_id

    def pending_segments(
        self,
        session: SessionKey,
        *,
        limit: int | None = 10,
    ) -> list[StoredSegment]:
        sql = (
            "SELECT * FROM segment WHERE session_key = ? AND consumed_at IS NULL "
            "ORDER BY created_at ASC"
        )
        params: list[Any] = [session.key]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        rows = self._connection.execute(sql, params).fetchall()
        return [self._to_segment(row) for row in rows]

    def pending_count(self, session: SessionKey) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM segment "
            "WHERE session_key = ? AND consumed_at IS NULL",
            (session.key,),
        ).fetchone()
        return int(row[0] if row else 0)

    def mark_consumed(
        self,
        segment_ids: Iterable[str],
        *,
        moment: datetime | None = None,
    ) -> int:
        ids = [str(item) for item in segment_ids if item]
        if not ids:
            return 0
        now = utc_now_iso(moment)
        placeholders = ",".join("?" for _ in ids)
        cursor = self._connection.execute(
            f"UPDATE segment SET consumed_at = ? "
            f"WHERE segment_id IN ({placeholders}) AND consumed_at IS NULL",
            [now, *ids],
        )
        self._connection.commit()
        return int(cursor.rowcount or 0)

    def skip_segments(
        self,
        segment_ids: Iterable[str],
        *,
        reason: str = "policy_skipped",
        moment: datetime | None = None,
    ) -> int:
        """政策拒绝的片段不再重试：标记为 skipped 并移出 pending。"""
        ids = [str(item) for item in segment_ids if item]
        if not ids:
            return 0
        now = utc_now_iso(moment)
        placeholders = ",".join("?" for _ in ids)
        cursor = self._connection.execute(
            f"UPDATE segment SET consumed_at = ?, status = ? "
            f"WHERE segment_id IN ({placeholders}) AND consumed_at IS NULL",
            [now, str(reason), *ids],
        )
        self._connection.commit()
        return int(cursor.rowcount or 0)

    def purge_expired(self, *, moment: datetime | None = None) -> int:
        now = utc_now_iso(moment)
        cursor = self._connection.execute(
            "DELETE FROM segment WHERE expires_at <= ?", (now,)
        )
        self._connection.commit()
        return int(cursor.rowcount or 0)

    # -- 画像 -----------------------------------------------------------

    def get_profile_session(self, session: SessionKey) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json FROM profile_session WHERE session_key = ?",
            (session.key,),
        ).fetchone()
        return _json_object(row["payload_json"]) if row else None

    def list_profile_sessions(
        self,
    ) -> list[tuple[SessionKey, dict[str, Any] | None, str]]:
        """所有有群画像或人物画像的 session，按最近更新排序。"""
        rows = self._connection.execute(
            "SELECT session_key, MAX(updated_at) AS updated_at FROM ("
            "  SELECT session_key, updated_at FROM profile_session"
            "  UNION ALL"
            "  SELECT session_key, updated_at FROM profile_person"
            ") GROUP BY session_key ORDER BY updated_at DESC"
        ).fetchall()
        sessions: list[tuple[SessionKey, dict[str, Any] | None, str]] = []
        for row in rows:
            try:
                session = SessionKey.parse(str(row["session_key"]))
            except (TypeError, ValueError):
                continue
            sessions.append(
                (
                    session,
                    self.get_profile_session(session),
                    str(row["updated_at"] or ""),
                )
            )
        return sessions

    def list_profile_persons(
        self, session: SessionKey
    ) -> list[tuple[str, str]]:
        rows = self._connection.execute(
            "SELECT sender_id, display_name FROM profile_person "
            "WHERE session_key = ? ORDER BY display_name ASC, sender_id ASC",
            (session.key,),
        ).fetchall()
        return [(str(row[0]), str(row[1] or "")) for row in rows]

    def get_profile_person(
        self, session: SessionKey, sender_id: str
    ) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json FROM profile_person "
            "WHERE session_key = ? AND sender_id = ?",
            (session.key, str(sender_id)),
        ).fetchone()
        return _json_object(row["payload_json"]) if row else None

    def save_profile_session(
        self,
        session: SessionKey,
        payload: dict[str, Any],
    ) -> None:
        now = utc_now_iso()
        self._connection.execute(
            "INSERT INTO profile_session (session_key, payload_json, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(session_key) DO UPDATE SET "
            "payload_json = excluded.payload_json, updated_at = excluded.updated_at",
            (
                session.key,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        self._connection.commit()

    def save_profile_person(
        self,
        session: SessionKey,
        sender_id: str,
        display_name: str,
        payload: dict[str, Any],
    ) -> None:
        now = utc_now_iso()
        sender_id = str(sender_id).strip()
        self._connection.execute(
            "INSERT INTO profile_person "
            "(session_key, sender_id, display_name, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_key, sender_id) DO UPDATE SET "
            "display_name = excluded.display_name, "
            "payload_json = excluded.payload_json, updated_at = excluded.updated_at",
            (
                session.key,
                sender_id,
                str(display_name or ""),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        self._connection.commit()

    def sessions_with_pending(self) -> list[SessionKey]:
        rows = self._connection.execute(
            "SELECT DISTINCT session_key FROM segment WHERE consumed_at IS NULL"
        ).fetchall()
        sessions: list[SessionKey] = []
        for row in rows:
            try:
                sessions.append(SessionKey.parse(str(row[0])))
            except ValueError:
                continue
        sessions.sort(key=lambda item: item.key)
        return sessions

    def commit_merge(
        self,
        session: SessionKey,
        *,
        group_payload: dict[str, Any] | None,
        persons: Sequence[tuple[str, str, dict[str, Any]]],
        segment_ids: Sequence[str],
        run_info: dict[str, Any],
    ) -> str:
        """一次合并事务：写画像 + 标记片段消费 + 记 merge_run。"""
        now = utc_now_iso()
        run_id = str(run_info.get("run_id") or "").strip()
        if not run_id:
            run_id = sha256_hex(
                session.key,
                run_info.get("run_date", ""),
                *[str(item) for item in segment_ids],
            )
        with self._connection:
            if group_payload is not None:
                self._connection.execute(
                    "INSERT INTO profile_session (session_key, payload_json, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(session_key) DO UPDATE SET "
                    "payload_json = excluded.payload_json, "
                    "updated_at = excluded.updated_at",
                    (
                        session.key,
                        json.dumps(group_payload, ensure_ascii=False),
                        now,
                    ),
                )
            for sender_id, display_name, payload in persons:
                self._connection.execute(
                    "INSERT INTO profile_person "
                    "(session_key, sender_id, display_name, payload_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_key, sender_id) DO UPDATE SET "
                    "display_name = CASE WHEN excluded.display_name <> '' "
                    "THEN excluded.display_name ELSE profile_person.display_name END, "
                    "payload_json = excluded.payload_json, "
                    "updated_at = excluded.updated_at",
                    (
                        session.key,
                        str(sender_id),
                        str(display_name or ""),
                        json.dumps(payload, ensure_ascii=False),
                        now,
                    ),
                )
            if segment_ids:
                placeholders = ",".join("?" for _ in segment_ids)
                self._connection.execute(
                    f"UPDATE segment SET consumed_at = ? "
                    f"WHERE segment_id IN ({placeholders}) AND consumed_at IS NULL",
                    [now, *[str(item) for item in segment_ids]],
                )
            self._connection.execute(
                "INSERT OR REPLACE INTO merge_run "
                "(run_id, run_date, session_key, segment_count, person_count, "
                " status, llm_status, error, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    str(run_info.get("run_date") or ""),
                    session.key,
                    int(run_info.get("segment_count") or len(segment_ids)),
                    int(run_info.get("person_count") or len(persons)),
                    str(run_info.get("status") or "success"),
                    str(run_info.get("llm_status") or ""),
                    str(run_info.get("error") or ""),
                    str(run_info.get("started_at") or now),
                    now,
                ),
            )
        return run_id

    def profile_counts(self) -> dict[str, int]:
        row = self._connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM profile_session), "
            "(SELECT COUNT(*) FROM profile_person), "
            "(SELECT COUNT(*) FROM segment WHERE consumed_at IS NULL)"
        ).fetchone()
        if not row:
            return {"sessions": 0, "persons": 0, "pending_segments": 0}
        return {
            "sessions": int(row[0]),
            "persons": int(row[1]),
            "pending_segments": int(row[2]),
        }

    # -- 测试/诊断 -------------------------------------------------------

    def count_segments(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM segment").fetchone()
        return int(row[0]) if row else 0

    def count_cursors(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM cursor").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "ProfileStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # -- 内部 -----------------------------------------------------------

    @staticmethod
    def _to_cursor(row: sqlite3.Row) -> Cursor:
        return Cursor(
            session_key=str(row["session_key"]),
            seq=int(row["seq"]),
            msg_uid=str(row["msg_uid"] or ""),
            generation=int(row["generation"]),
            schema_ver=int(row["schema_ver"]),
            dual_started=str(row["dual_started"] or ""),
            last_status=str(row["last_status"] or ""),
        )

    @staticmethod
    def _to_segment(row: sqlite3.Row) -> StoredSegment:
        return StoredSegment(
            segment_id=str(row["segment_id"]),
            session_key=str(row["session_key"]),
            start_seq=int(row["start_seq"]),
            end_seq=int(row["end_seq"]),
            message_count=int(row["message_count"]),
            status=str(row["status"]),
            summary_json=str(row["summary_json"]),
            created_at=str(row["created_at"]),
            consumed_at=str(row["consumed_at"] or ""),
            expires_at=str(row["expires_at"]),
        )
