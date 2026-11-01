"""运行时端到端：扫描 → 片段落库 → 合并 → 画像可查。"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from profile_core.config import Settings
from profile_core.contracts import SessionKey
from profile_core.llm import LlmError, LlmResult
from profile_core.runtime import Runtime

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


class ScriptedLlm:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def complete(self, **kwargs) -> LlmResult:
        self.prompts.append(kwargs["prompt"])
        if not self.responses:
            raise LlmError("no script")
        return LlmResult(self.responses.pop(0), {})


def _make_cm(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    con.execute("INSERT INTO chat_memory_shadow_meta VALUES ('schema_version', '3')")
    con.execute(
        "INSERT INTO chat_memory_shadow_meta VALUES ('dual_write_started_at', 't0')"
    )
    for seq, sender in ((1, "10001"), (2, "10002"), (3, "10001")):
        con.execute(
            "INSERT INTO chat_memory_messages "
            "(msg_uid, seq, platform_name, message_type, session_id, sender_id, "
            "sender_nickname, platform_timestamp, content, content_kind, "
            "relation_data, reply_group, platform_seq) "
            "VALUES (?, ?, 'demo', 'GroupMessage', 'group_demo', ?, 'A', ?, ?, "
            "'[\"text\"]', '{}', '', ?)",
            (
                f"p:demo:group_demo:{seq}",
                seq,
                sender,
                1_700_000_000 + seq,
                f"m{seq}",
                seq,
            ),
        )
    con.commit()
    con.close()


def _settings(tmp: Path, **overrides) -> Settings:
    base = dict(
        extract_threshold=2,
        initial_cursor="oldest",
        flush_min_units=10,
        flush_time="00:00",
        merge_time="00:00",
        tick_seconds=3600,
        max_segments_per_scan=10,
    )
    base.update(overrides)
    return Settings(**base)


def test_scan_then_merge_end_to_end(tmp_path: Path) -> None:
    _make_cm(tmp_path / "cm.db")
    llm = ScriptedLlm(
        [
            json.dumps(
                {
                    "persons": [
                        {
                            "speaker": "P1",
                            "name": "A",
                            "sections": {"identity": "在杭州"},
                        }
                    ],
                    "group": {"theme": "技术"},
                    "mentioned": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {"theme": "技术", "atmosphere": "轻松"}, ensure_ascii=False
            ),
            json.dumps({"identity": "在杭州"}, ensure_ascii=False),
        ]
    )
    runtime = Runtime(
        None,
        _settings(tmp_path),
        logger=logging.getLogger("test"),
        data_dir=str(tmp_path),
        llm=llm,
        cm_db_path=str(tmp_path / "cm.db"),
        profile_db_path=str(tmp_path / "profile.db"),
    )

    async def scenario() -> None:
        await runtime.start(auto_loop=False)
        assert runtime.ready
        await runtime.scan_now()
        cursor = runtime.store.get_cursor(SESSION)
        assert cursor is not None and cursor.seq >= 2
        assert runtime.store.profile_counts()["pending_segments"] == 1
        await runtime.merge_now()
        assert runtime.store.get_profile_session(SESSION) == {
            "long": {"theme": "技术", "atmosphere": "轻松"}
        }
        assert runtime.store.get_profile_person(SESSION, "10001") == {
            "long": {"identity": "在杭州"}
        }
        assert runtime.store.profile_counts()["pending_segments"] == 0
        await runtime.stop()

    asyncio.run(scenario())
    assert len(llm.prompts) == 3


def test_extraction_repeated_failures_advance_cursor(tmp_path: Path) -> None:
    _make_cm(tmp_path / "cm.db")
    runtime = Runtime(
        None,
        _settings(
            tmp_path,
            failure_advance_after=2,
            extract_refusal_advance_messages=1,
        ),
        logger=logging.getLogger("test"),
        data_dir=str(tmp_path),
        llm=ScriptedLlm([]),
        cm_db_path=str(tmp_path / "cm.db"),
        profile_db_path=str(tmp_path / "profile.db"),
    )

    async def scenario() -> None:
        await runtime.start(auto_loop=False)
        await runtime.scan_now()
        assert runtime.store.get_cursor(SESSION).seq == 0
        await runtime.scan_now()
        assert runtime.store.get_cursor(SESSION).seq == 1
        await runtime.stop()

    asyncio.run(scenario())


def test_merge_repeated_failures_skip_pending(tmp_path: Path) -> None:
    _make_cm(tmp_path / "cm.db")
    llm = ScriptedLlm(
        [
            json.dumps(
                {
                    "persons": [
                        {
                            "speaker": "P1",
                            "name": "A",
                            "sections": {"identity": "在杭州"},
                        }
                    ],
                    "group": {"theme": "技术"},
                    "mentioned": [],
                },
                ensure_ascii=False,
            )
        ]
    )
    runtime = Runtime(
        None,
        _settings(
            tmp_path,
            failure_advance_after=2,
            merge_refusal_advance_segments=5,
        ),
        logger=logging.getLogger("test"),
        data_dir=str(tmp_path),
        llm=llm,
        cm_db_path=str(tmp_path / "cm.db"),
        profile_db_path=str(tmp_path / "profile.db"),
    )

    async def scenario() -> None:
        await runtime.start(auto_loop=False)
        await runtime.scan_now()
        assert runtime.store.profile_counts()["pending_segments"] == 1
        assert await runtime.merge_now() is False
        assert await runtime.merge_now() is True
        assert runtime.store.profile_counts()["pending_segments"] == 0
        assert runtime.store.get_profile_session(SESSION) is None
        await runtime.stop()

    asyncio.run(scenario())

def test_backlog_counts_unprocessed_messages(tmp_path: Path) -> None:
    _make_cm(tmp_path / "cm.db")
    runtime = Runtime(
        None,
        _settings(tmp_path),
        logger=logging.getLogger("test"),
        data_dir=str(tmp_path),
        llm=ScriptedLlm(
            [json.dumps({"persons": [], "group": {}, "mentioned": []})]
        ),
        cm_db_path=str(tmp_path / "cm.db"),
        profile_db_path=str(tmp_path / "profile.db"),
    )

    async def scenario() -> None:
        await runtime.start(auto_loop=False)
        assert runtime._backlog() == (1, 3, 0)
        await runtime.scan_now()
        assert runtime._backlog() == (1, 1, 1)
        await runtime.stop()

    asyncio.run(scenario())


def test_extraction_retries_failed_json_immediately(tmp_path: Path) -> None:
    _make_cm(tmp_path / "cm.db")
    llm = ScriptedLlm(
        [
            "not json",
            json.dumps(
                {
                    "persons": [
                        {
                            "speaker": "P1",
                            "name": "A",
                            "sections": {"identity": "在杭州"},
                        }
                    ],
                    "group": {"theme": "技术"},
                    "mentioned": [],
                },
                ensure_ascii=False,
            ),
        ]
    )
    runtime = Runtime(
        None,
        _settings(tmp_path),
        logger=logging.getLogger("test"),
        data_dir=str(tmp_path),
        llm=llm,
        cm_db_path=str(tmp_path / "cm.db"),
        profile_db_path=str(tmp_path / "profile.db"),
    )

    async def scenario() -> None:
        await runtime.start(auto_loop=False)
        await runtime.scan_now()
        assert runtime.store.profile_counts()["pending_segments"] == 1
        await runtime.stop()

    asyncio.run(scenario())
    assert len(llm.prompts) == 2


def test_initial_cursor_latest_skips_history(tmp_path: Path) -> None:
    _make_cm(tmp_path / "cm.db")
    llm = ScriptedLlm([])
    runtime = Runtime(
        None,
        _settings(tmp_path, initial_cursor="latest"),
        logger=logging.getLogger("test"),
        data_dir=str(tmp_path),
        llm=llm,
        cm_db_path=str(tmp_path / "cm.db"),
        profile_db_path=str(tmp_path / "profile.db"),
    )

    async def scenario() -> None:
        await runtime.start(auto_loop=False)
        await runtime.scan_now()
        cursor = runtime.store.get_cursor(SESSION)
        assert cursor is not None and cursor.seq == 3
        assert runtime.store.profile_counts()["pending_segments"] == 0
        await runtime.stop()

    asyncio.run(scenario())
    assert llm.prompts == []


def test_extraction_failure_does_not_advance_cursor(tmp_path: Path) -> None:
    _make_cm(tmp_path / "cm.db")
    runtime = Runtime(
        None,
        _settings(tmp_path),
        logger=logging.getLogger("test"),
        data_dir=str(tmp_path),
        llm=ScriptedLlm([]),
        cm_db_path=str(tmp_path / "cm.db"),
        profile_db_path=str(tmp_path / "profile.db"),
    )

    async def scenario() -> None:
        await runtime.start(auto_loop=False)
        await runtime.scan_now()
        cursor = runtime.store.get_cursor(SESSION)
        assert cursor is not None and cursor.seq == 0
        assert runtime.store.profile_counts()["pending_segments"] == 0
        await runtime.stop()

    asyncio.run(scenario())
