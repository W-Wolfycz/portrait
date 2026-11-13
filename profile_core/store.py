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

CREATE TABLE IF NOT EXISTS profile_field_meta (
  session_key  TEXT NOT NULL,
  subject      TEXT NOT NULL,
  tier         TEXT NOT NULL,
  field        TEXT NOT NULL,
  value_hash   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (session_key, subject, tier, field)
);

CREATE INDEX IF NOT EXISTS ix_profile_field_meta_subject
  ON profile_field_meta (session_key, subject);

CREATE TABLE IF NOT EXISTS merge_run (
  run_id        TEXT PRIMARY KEY,
  run_date      TEXT NOT NULL,
  session_key   TEXT NOT NULL,
  segment_count  INTEGER NOT NULL DEFAULT 0,
  person_count  INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL,
  llm_status    TEXT NOT NULL DEFAULT '',
  error         TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  usage_json    TEXT NOT NULL DEFAULT '{}',
  started_at    TEXT NOT NULL,
  finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS ix_merge_run_session
  ON merge_run (session_key, run_date);

CREATE TABLE IF NOT EXISTS profile_version (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key  TEXT NOT NULL,
  subject      TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  reason       TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_profile_version_subject
  ON profile_version (session_key, subject, id);

CREATE TABLE IF NOT EXISTS merge_diff (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT NOT NULL,
  session_key  TEXT NOT NULL,
  subject      TEXT NOT NULL,
  diff_json    TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_merge_diff_subject
  ON merge_diff (session_key, subject, id);

CREATE TABLE IF NOT EXISTS merge_failure (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key   TEXT NOT NULL,
  error_type    TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT '',
  segment_count INTEGER NOT NULL DEFAULT 0,
  day           TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_merge_failure_time
  ON merge_failure (created_at);
"""

#: 旧库增量迁移：给已存在的表补新列（新库由 _SCHEMA 直接建好）。
_ALTER_COLUMNS = (
    ("merge_run", "prompt_version", "TEXT NOT NULL DEFAULT ''"),
    ("merge_run", "usage_json", "TEXT NOT NULL DEFAULT '{}'"),
    (
        "profile_field_meta",
        "prev_hash",
        "TEXT NOT NULL DEFAULT ''",
    ),
    (
        "profile_field_meta",
        "flip_count",
        "INTEGER NOT NULL DEFAULT 0",
    ),
    ("merge_failure", "day", "TEXT NOT NULL DEFAULT ''"),
)

GROUP_SUBJECT = "group"


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


def _days_since(iso_text: str, now: datetime) -> int:
    """把一个 ISO 时间戳换算成距 ``now`` 的整天数；坏值按 0 处理。"""
    try:
        then = datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, int((now - then).total_seconds() // 86400))


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
        for table, column, decl in _ALTER_COLUMNS:
            try:
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )
            except sqlite3.OperationalError as exc:
                # duplicate column name：新库已由 _SCHEMA 建好，直接跳过。
                if "duplicate column" not in str(exc).casefold():
                    raise
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
        """schema/dual 元信息变化时只重绑标记，不推翻已有游标。

        真正的 seq/uid 重置交给 detect_reset 判断。
        """
        schema_ver = _safe_int(shadow_meta.get("schema_version"), 0)
        dual_started = str(
            shadow_meta.get("dual_write_started_at") or ""
        ).strip()
        if not schema_ver and not dual_started:
            return 0
        now = utc_now_iso(moment)
        cursor = self._connection.execute(
            "UPDATE cursor SET schema_ver = ?, dual_started = ?, "
            "last_status = 'schema_rebind', last_error = '', updated_at = ? "
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

    def purge_expired(
        self,
        *,
        moment: datetime | None = None,
        consumed_retention_days: int = 7,
    ) -> int:
        now = moment or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now_text = utc_now_iso(now)
        cursor = self._connection.execute(
            "DELETE FROM segment "
            "WHERE consumed_at IS NULL AND expires_at <= ?",
            (now_text,),
        )
        removed = int(cursor.rowcount or 0)
        days = int(consumed_retention_days)
        if days > 0:
            cutoff = utc_now_iso(now - timedelta(days=days))
            cursor = self._connection.execute(
                "DELETE FROM segment "
                "WHERE consumed_at IS NOT NULL AND consumed_at <= ?",
                (cutoff,),
            )
            removed += int(cursor.rowcount or 0)
        self._connection.commit()
        return removed

    def list_segments(
        self,
        session: SessionKey,
        *,
        include_consumed: bool = True,
        limit: int = 100,
    ) -> list[StoredSegment]:
        """按时间倒序读取片段摘要，供日报/调试公共接口使用。"""
        sql = "SELECT * FROM segment WHERE session_key = ?"
        params: list[Any] = [session.key]
        if not include_consumed:
            sql += " AND consumed_at IS NULL"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self._connection.execute(sql, params).fetchall()
        return [self._to_segment(row) for row in rows]

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
        *,
        reason: str = "manual",
    ) -> None:
        now = utc_now_iso()
        previous = self.get_profile_session(session)
        if previous is not None and previous != payload:
            self._snapshot_version(
                session, GROUP_SUBJECT, previous, reason
            )
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
        self.sync_field_meta(session, GROUP_SUBJECT, payload)

    def save_profile_person(
        self,
        session: SessionKey,
        sender_id: str,
        display_name: str,
        payload: dict[str, Any],
        *,
        reason: str = "manual",
    ) -> None:
        now = utc_now_iso()
        sender_id = str(sender_id).strip()
        previous = self.get_profile_person(session, sender_id)
        if previous is not None and previous != payload:
            self._snapshot_version(session, sender_id, previous, reason)
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
        self.sync_field_meta(session, sender_id, payload)

    def sync_field_meta(
        self,
        session: SessionKey,
        subject: str,
        payload: dict[str, Any],
        *,
        moment: datetime | None = None,
    ) -> None:
        """按新旧画像 diff 刷新字段时效；消失的字段一并清理。

        只有字段内容变化才推进 ``updated_at``。值没动说明这一轮没有带来
        新信息（可能压根没人被提到），保守地保持旧时间，避免"假新鲜"。
        """
        now = utc_now_iso(moment)
        subject = str(subject or "").strip() or GROUP_SUBJECT
        current: dict[tuple[str, str], str] = {}
        for tier, sections in (payload or {}).items():
            if not isinstance(sections, dict):
                continue
            for field, value in sections.items():
                text = str(value or "").strip()
                if text:
                    current[(str(tier), str(field))] = sha256_hex(text)

        rows = self._connection.execute(
            "SELECT tier, field, value_hash, prev_hash, flip_count, updated_at "
            "FROM profile_field_meta WHERE session_key = ? AND subject = ?",
            (session.key, subject),
        ).fetchall()
        previous = {
            (str(row["tier"]), str(row["field"])): (
                str(row["value_hash"]),
                str(row["prev_hash"] or ""),
                int(row["flip_count"] or 0),
            )
            for row in rows
        }

        with self._connection:
            for key, digest in current.items():
                tier, field = key
                old = previous.pop(key, None)
                if old is not None and old[0] == digest:
                    continue
                if old is None:
                    flips = 0
                elif digest == old[1] and old[1]:
                    # 值翻回了上上次的内容——字段在横跳。
                    flips = old[2] + 1
                else:
                    flips = old[2]
                self._connection.execute(
                    "INSERT INTO profile_field_meta "
                    "(session_key, subject, tier, field, value_hash, "
                    " prev_hash, flip_count, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_key, subject, tier, field) DO UPDATE SET "
                    "value_hash = excluded.value_hash, "
                    "prev_hash = excluded.prev_hash, "
                    "flip_count = excluded.flip_count, "
                    "updated_at = excluded.updated_at",
                    (
                        session.key,
                        subject,
                        tier,
                        field,
                        digest,
                        old[0] if old else "",
                        flips,
                        now,
                    ),
                )
            for tier, field in previous:
                self._connection.execute(
                    "DELETE FROM profile_field_meta WHERE session_key = ? "
                    "AND subject = ? AND tier = ? AND field = ?",
                    (session.key, subject, tier, field),
                )

    def field_ages(
        self,
        session: SessionKey,
        subject: str,
        *,
        moment: datetime | None = None,
    ) -> dict[tuple[str, str], int]:
        """返回 ``{(tier, field): 距上次内容变化的天数}``。"""
        now = moment or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        subject = str(subject or "").strip() or GROUP_SUBJECT
        rows = self._connection.execute(
            "SELECT tier, field, updated_at FROM profile_field_meta "
            "WHERE session_key = ? AND subject = ?",
            (session.key, subject),
        ).fetchall()
        return {
            (str(row["tier"]), str(row["field"])): _days_since(
                str(row["updated_at"]), now
            )
            for row in rows
        }

    def field_flips(
        self,
        session: SessionKey,
        subject: str,
    ) -> dict[tuple[str, str], int]:
        """返回 ``{(tier, field): 横跳次数}``，只含 >=2 次的字段。"""
        subject = str(subject or "").strip() or GROUP_SUBJECT
        rows = self._connection.execute(
            "SELECT tier, field, flip_count FROM profile_field_meta "
            "WHERE session_key = ? AND subject = ? AND flip_count >= 2",
            (session.key, subject),
        ).fetchall()
        return {
            (str(row["tier"]), str(row["field"])): int(row["flip_count"] or 0)
            for row in rows
        }

    # -- 版本快照 / 合并 diff / 删除 ------------------------------------

    #: 每个 subject 保留的快照份数。
    _VERSIONS_KEPT = 20
    #: 每个 subject 保留的 diff 条数（避免表无限增长）。
    _DIFFS_KEPT = 50
    #: 失败记录保留条数（全局）。
    _FAILURES_KEPT = 500

    def _snapshot_version(
        self,
        session: SessionKey,
        subject: str,
        payload: dict[str, Any] | None,
        reason: str,
    ) -> None:
        """画像被覆盖前留一份快照；内容没变不留，超出上限删旧。"""
        if not payload:
            return
        subject = str(subject or "").strip() or GROUP_SUBJECT
        self._connection.execute(
            "INSERT INTO profile_version "
            "(session_key, subject, payload_json, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                session.key,
                subject,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                str(reason or ""),
                utc_now_iso(),
            ),
        )
        self._prune_versions(session, subject)

    def _prune_versions(
        self, session: SessionKey, subject: str
    ) -> None:
        self._connection.execute(
            "DELETE FROM profile_version WHERE id IN ("
            "  SELECT id FROM profile_version"
            "  WHERE session_key = ? AND subject = ?"
            "  ORDER BY id DESC LIMIT -1 OFFSET ?"
            ")",
            (session.key, subject, self._VERSIONS_KEPT),
        )

    def list_versions(
        self,
        session: SessionKey,
        subject: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        subject = str(subject or "").strip() or GROUP_SUBJECT
        rows = self._connection.execute(
            "SELECT id, reason, created_at FROM profile_version "
            "WHERE session_key = ? AND subject = ? ORDER BY id DESC LIMIT ?",
            (session.key, subject, max(1, int(limit))),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "reason": str(row["reason"] or ""),
                "created_at": str(row["created_at"] or ""),
            }
            for row in rows
        ]

    def get_version_payload(
        self, session: SessionKey, subject: str, version_id: int
    ) -> dict[str, Any] | None:
        subject = str(subject or "").strip() or GROUP_SUBJECT
        row = self._connection.execute(
            "SELECT payload_json FROM profile_version "
            "WHERE id = ? AND session_key = ? AND subject = ?",
            (int(version_id), session.key, subject),
        ).fetchone()
        return _json_object(row["payload_json"]) if row else None

    def delete_profile_person(
        self, session: SessionKey, sender_id: str
    ) -> int:
        """删除某人物画像（含时效、版本与 diff）；返回是否有行被删。"""
        sender_id = str(sender_id or "").strip()
        if not sender_id:
            return 0
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM profile_person "
                "WHERE session_key = ? AND sender_id = ?",
                (session.key, sender_id),
            )
            removed = int(cursor.rowcount or 0)
            self._connection.execute(
                "DELETE FROM profile_field_meta WHERE session_key = ? "
                "AND subject = ?",
                (session.key, sender_id),
            )
            self._connection.execute(
                "DELETE FROM profile_version WHERE session_key = ? "
                "AND subject = ?",
                (session.key, sender_id),
            )
            self._connection.execute(
                "DELETE FROM merge_diff WHERE session_key = ? AND subject = ?",
                (session.key, sender_id),
            )
        return removed

    def delete_profile_group(self, session: SessionKey) -> int:
        """删除群画像本体（人物画像不动）。"""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM profile_session WHERE session_key = ?",
                (session.key,),
            )
            removed = int(cursor.rowcount or 0)
            self._connection.execute(
                "DELETE FROM profile_field_meta WHERE session_key = ? "
                "AND subject = ?",
                (session.key, GROUP_SUBJECT),
            )
            self._connection.execute(
                "DELETE FROM profile_version WHERE session_key = ? "
                "AND subject = ?",
                (session.key, GROUP_SUBJECT),
            )
            self._connection.execute(
                "DELETE FROM merge_diff WHERE session_key = ? AND subject = ?",
                (session.key, GROUP_SUBJECT),
            )
        return removed

    def latest_merge_diff(
        self,
        session: SessionKey,
        subject: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        subject = str(subject or "").strip() or GROUP_SUBJECT
        rows = self._connection.execute(
            "SELECT run_id, diff_json, created_at FROM merge_diff "
            "WHERE session_key = ? AND subject = ? ORDER BY id DESC LIMIT ?",
            (session.key, subject, max(1, int(limit))),
        ).fetchall()
        return [
            {
                "run_id": str(row["run_id"] or ""),
                "diff": _json_object(row["diff_json"]),
                "created_at": str(row["created_at"] or ""),
            }
            for row in rows
        ]

    # -- 运行状态 / 失败记录 --------------------------------------------

    def set_stat(self, key: str, value: Any) -> None:
        """写一条运行时统计（如 last_scan_at），带写入时间。"""
        self._connection.execute(
            "INSERT INTO runtime_stat (k, v, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(k) DO UPDATE SET "
            "v = excluded.v, updated_at = excluded.updated_at",
            (str(key), str(value), utc_now_iso()),
        )
        self._connection.commit()

    def get_stat(self, key: str) -> str:
        row = self._connection.execute(
            "SELECT v FROM runtime_stat WHERE k = ?", (str(key),)
        ).fetchone()
        return str(row["v"] or "") if row else ""

    def record_merge_failure(
        self,
        session: SessionKey,
        error_type: str,
        *,
        kind: str = "",
        segment_count: int = 0,
        day: str = "",
        moment: datetime | None = None,
    ) -> None:
        """记一次合并失败。

        合并失败会直接 return/skip，从不走 commit_merge，所以失败信息
        必须单独落库，否则健康面板与成本分析里完全看不到。
        只记异常类型名，不记异常正文（可能含聊天内容）。

        ``day`` 必须是配置时区的自然日，与 ``merge_run.run_date`` 同一口径；
        缺省时退回 UTC 日期，跨时区会与成功合并的日子错位。
        """
        self._connection.execute(
            "INSERT INTO merge_failure "
            "(session_key, error_type, kind, segment_count, day, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session.key,
                str(error_type or "Unknown")[:64],
                str(kind or "")[:32],
                max(0, int(segment_count)),
                str(day or "")[:10],
                utc_now_iso(moment),
            ),
        )
        self._prune_failures()
        self._connection.commit()

    def _prune_failures(self) -> None:
        """失败记录只保留最近 N 条，否则长期运行会无限增长。"""
        self._connection.execute(
            "DELETE FROM merge_failure WHERE id NOT IN ("
            "  SELECT id FROM merge_failure ORDER BY id DESC LIMIT ?"
            ")",
            (self._FAILURES_KEPT,),
        )

    def failure_summary(
        self, *, days: int = 7, limit: int = 8
    ) -> dict[str, Any]:
        """按错误类型聚合最近失败；用于 dashboard 的错误面板。"""
        now = datetime.now(timezone.utc)
        since = utc_now_iso(now - timedelta(days=max(1, days)))
        rows = self._connection.execute(
            "SELECT error_type, kind, COUNT(*) AS n, MAX(created_at) AS last_at, "
            "SUM(segment_count) AS segments FROM merge_failure "
            "WHERE created_at >= ? "
            "GROUP BY error_type, kind ORDER BY n DESC, last_at DESC LIMIT ?",
            (since, max(1, int(limit))),
        ).fetchall()
        by_type = [
            {
                "error_type": str(row["error_type"] or ""),
                "kind": str(row["kind"] or ""),
                "count": int(row["n"] or 0),
                "segments": int(row["segments"] or 0),
                "last_at": str(row["last_at"] or ""),
            }
            for row in rows
        ]
        total_row = self._connection.execute(
            "SELECT COUNT(*) FROM merge_failure WHERE created_at >= ?",
            (since,),
        ).fetchone()
        recent = self._connection.execute(
            "SELECT session_key, error_type, kind, segment_count, created_at "
            "FROM merge_failure ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return {
            "window_days": max(1, days),
            "total": int(total_row[0] or 0) if total_row else 0,
            "by_type": by_type,
            "recent": [
                {
                    "session": str(row["session_key"] or ""),
                    "error_type": str(row["error_type"] or ""),
                    "kind": str(row["kind"] or ""),
                    "segments": int(row["segment_count"] or 0),
                    "created_at": str(row["created_at"] or ""),
                }
                for row in recent
            ],
        }

    def health_summary(self, *, days: int = 14) -> dict[str, Any]:
        """运行健康聚合：积压、合并成败、token 消耗、字段年龄、失败明细。"""
        now = datetime.now(timezone.utc)
        pending_row = self._connection.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest "
            "FROM segment WHERE consumed_at IS NULL"
        ).fetchone()
        pending = int(pending_row["n"] or 0) if pending_row else 0
        oldest_days = 0
        if pending_row and pending_row["oldest"]:
            oldest_days = _days_since(str(pending_row["oldest"]), now)

        since = utc_now_iso(now - timedelta(days=max(1, days)))
        rows = self._connection.execute(
            "SELECT run_date, COUNT(*) AS runs, "
            "SUM(CASE WHEN status <> 'success' THEN 1 ELSE 0 END) AS failed, "
            "SUM(segment_count) AS segments, SUM(person_count) AS persons, "
            "COALESCE(SUM(json_extract(usage_json, '$.prompt_tokens')), 0) AS pt, "
            "COALESCE(SUM(json_extract(usage_json, '$.completion_tokens')), 0) "
            "AS ct "
            "FROM merge_run WHERE finished_at >= ? "
            "GROUP BY run_date ORDER BY run_date DESC",
            (since,),
        ).fetchall()
        # 失败只来自 merge_failure：合并失败从不写 merge_run，
        # 拿 merge_run.status 统计会永远得到 0。
        # 归日优先用 day（配置时区），旧数据回退 UTC 日期。
        fail_rows = self._connection.execute(
            "SELECT COALESCE(NULLIF(day, ''), substr(created_at, 1, 10)) AS d, "
            "COUNT(*) AS n FROM merge_failure WHERE created_at >= ? GROUP BY d",
            (since,),
        ).fetchall()
        fail_by_day = {
            str(row["d"] or ""): int(row["n"] or 0) for row in fail_rows
        }
        daily_map: dict[str, dict[str, Any]] = {}
        for row in rows:
            day = str(row["run_date"] or "")
            daily_map[day] = {
                "date": day,
                "runs": int(row["runs"] or 0),
                "failed": fail_by_day.get(day, 0),
                "segments": int(row["segments"] or 0),
                "persons": int(row["persons"] or 0),
                "prompt_tokens": int(row["pt"] or 0),
                "completion_tokens": int(row["ct"] or 0),
            }
        # 只有失败没有成功合并的日子，也必须出现在趋势里，否则看不见。
        for day, failed in fail_by_day.items():
            if day in daily_map:
                continue
            daily_map[day] = {
                "date": day,
                "runs": 0,
                "failed": failed,
                "segments": 0,
                "persons": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }
        daily = sorted(
            daily_map.values(), key=lambda item: item["date"], reverse=True
        )

        age_rows = self._connection.execute(
            "SELECT updated_at FROM profile_field_meta"
        ).fetchall()
        ages = [_days_since(str(row["updated_at"]), now) for row in age_rows]
        flip_rows = self._connection.execute(
            "SELECT COUNT(*) FROM profile_field_meta WHERE flip_count >= 2"
        ).fetchone()
        counts = self.profile_counts()
        scan_at = self.get_stat("last_scan_at")
        scan_age = _days_since(scan_at, now) if scan_at else None

        return {
            "pending_segments": pending,
            "pending_oldest_days": oldest_days,
            "profiles": counts,
            "field_count": len(ages),
            "field_age_avg_days": (
                round(sum(ages) / len(ages), 1) if ages else 0
            ),
            "field_age_max_days": max(ages) if ages else 0,
            "unstable_fields": int(flip_rows[0] or 0) if flip_rows else 0,
            "last_scan_at": scan_at,
            "last_scan_age_days": scan_age,
            "failures": self.failure_summary(days=7),
            "daily": daily,
        }

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
        """一次合并事务：写画像 + 快照 + diff + 标记片段消费 + 记 merge_run。"""
        now = utc_now_iso()
        run_id = str(run_info.get("run_id") or "").strip()
        if not run_id:
            run_id = sha256_hex(
                session.key,
                run_info.get("run_date", ""),
                *[str(item) for item in segment_ids],
            )
        previous_group = (
            self.get_profile_session(session)
            if group_payload is not None
            else None
        )
        previous_persons = {
            str(sender_id): self.get_profile_person(session, sender_id)
            for sender_id, _name, _payload in persons
        }
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
                " status, llm_status, error, prompt_version, usage_json, "
                " started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    str(run_info.get("run_date") or ""),
                    session.key,
                    int(run_info.get("segment_count") or len(segment_ids)),
                    int(run_info.get("person_count") or len(persons)),
                    str(run_info.get("status") or "success"),
                    str(run_info.get("llm_status") or ""),
                    str(run_info.get("error") or ""),
                    str(run_info.get("prompt_version") or ""),
                    json.dumps(run_info.get("usage") or {}, ensure_ascii=False),
                    str(run_info.get("started_at") or now),
                    now,
                ),
            )
        if group_payload is not None:
            self._snapshot_version(
                session, GROUP_SUBJECT, previous_group, f"merge:{run_id[:8]}"
            )
            self._record_diff(
                session, GROUP_SUBJECT, previous_group, group_payload, run_id
            )
            self.sync_field_meta(session, GROUP_SUBJECT, group_payload)
        for sender_id, _display_name, payload in persons:
            sender_id = str(sender_id)
            self._snapshot_version(
                session,
                sender_id,
                previous_persons.get(sender_id),
                f"merge:{run_id[:8]}",
            )
            self._record_diff(
                session,
                sender_id,
                previous_persons.get(sender_id),
                payload,
                run_id,
            )
            self.sync_field_meta(session, sender_id, payload)
        return run_id

    @staticmethod
    def _flatten(payload: dict[str, Any] | None) -> dict[tuple[str, str], str]:
        result: dict[tuple[str, str], str] = {}
        for tier, sections in (payload or {}).items():
            if not isinstance(sections, dict):
                continue
            for field, value in sections.items():
                text = str(value or "").strip()
                if text:
                    result[(str(tier), str(field))] = text
        return result

    def _record_diff(
        self,
        session: SessionKey,
        subject: str,
        previous: dict[str, Any] | None,
        current: dict[str, Any],
        run_id: str,
    ) -> None:
        """对比覆盖前后画像，落一份 added/removed/changed/moved diff。"""
        old = self._flatten(previous)
        new = self._flatten(current)
        old_by_field: dict[str, tuple[str, str]] = {
            field: (tier, field) for tier, field in old
        }
        new_by_field: dict[str, tuple[str, str]] = {
            field: (tier, field) for tier, field in new
        }
        added: dict[str, str] = {}
        removed: dict[str, str] = {}
        changed: dict[str, dict[str, str]] = {}
        moved: dict[str, str] = {}
        for key, value in new.items():
            tier, field = key
            if field not in old_by_field:
                added[f"{tier}.{field}"] = value
                continue
            old_key = old_by_field[field]
            if old_key != key:
                moved[field] = f"{old_key[0]}→{tier}"
                if old[old_key] != value:
                    changed[f"{tier}.{field}"] = {
                        "old": old[old_key],
                        "new": value,
                    }
            elif old[old_key] != value:
                changed[f"{tier}.{field}"] = {"old": old[key], "new": value}
        for key, value in old.items():
            tier, field = key
            if field not in new_by_field:
                removed[f"{tier}.{field}"] = value
        if not (added or removed or changed or moved):
            return
        subject = str(subject or "").strip() or GROUP_SUBJECT
        self._connection.execute(
            "INSERT INTO merge_diff "
            "(run_id, session_key, subject, diff_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(run_id),
                session.key,
                subject,
                json.dumps(
                    {
                        "added": added,
                        "removed": removed,
                        "changed": changed,
                        "moved": moved,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                utc_now_iso(),
            ),
        )
        self._prune_diffs(session, subject)
        self._connection.commit()

    def _prune_diffs(self, session: SessionKey, subject: str) -> None:
        """diff 只保留最近 N 条，否则每轮合并都累加、表无限增长。"""
        self._connection.execute(
            "DELETE FROM merge_diff WHERE id IN ("
            "  SELECT id FROM merge_diff"
            "  WHERE session_key = ? AND subject = ?"
            "  ORDER BY id DESC LIMIT -1 OFFSET ?"
            ")",
            (session.key, subject, self._DIFFS_KEPT),
        )

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
