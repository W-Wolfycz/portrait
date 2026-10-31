"""可直接运行的最小示例：假 CM 库 + 假 LLM，跑通「读窗口 → 萃取 → 落库」。

    python example.py

输出会展示：建基线 → 阈值不足 → 达到阈值后提交 → 游标与条目状态 → ticker 的加速/降速决策。
不联网、不读取任何真实运行数据（库建在系统临时目录里，退出即删）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

# 允许 `python example.py` 直接运行（把包所在的父目录加入 sys.path）。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cm_pipeline import (
    AdaptiveTicker,
    ExtractionSpec,
    FakeLlmClient,
    Pipeline,
    PipelineConfig,
    SqliteSink,
    TickerConfig,
    WindowExtractor,
    open_cm_source,
)

# 与 chat_memory/shared_store.py 对齐的最小子集。
SHADOW_SCHEMA = """
CREATE TABLE chat_memory_messages (
    msg_uid TEXT PRIMARY KEY, seq INTEGER NOT NULL,
    platform_name TEXT NOT NULL, message_type TEXT NOT NULL, session_id TEXT NOT NULL,
    sender_id TEXT NOT NULL, sender_nickname TEXT NOT NULL DEFAULT '',
    platform_timestamp INTEGER NOT NULL DEFAULT 0, content TEXT NOT NULL DEFAULT '',
    content_kind TEXT NOT NULL DEFAULT '[]', recalled_at DATETIME, deleted_at DATETIME,
    created_at DATETIME NOT NULL
);
CREATE TABLE chat_memory_message_bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, msg_uid TEXT NOT NULL,
    platform_id TEXT NOT NULL, self_id TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT '', umo TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL, turn_id TEXT, llm_status TEXT NOT NULL DEFAULT '',
    persona_id TEXT, created_at DATETIME NOT NULL
);
CREATE TABLE chat_memory_shadow_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def build_demo_cm(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(SHADOW_SCHEMA)
    for seq, role, sender, nickname, content, turn in (
        (1, "user", "10001", "小明", "明天想去打球", "turn-1"),
        (2, "assistant", "10000", "某bot", "好啊，几点？", "turn-1"),
        (3, "user", "10001", "小明", "下午三点吧", "turn-2"),
        (4, "assistant", "10000", "某bot", "收到", "turn-2"),
    ):
        uid = f"p:qq:GroupMessage:group_demo:{seq}"
        connection.execute(
            "INSERT INTO chat_memory_messages (msg_uid, seq, platform_name, "
            " message_type, session_id, sender_id, sender_nickname, "
            " platform_timestamp, content, content_kind, created_at) "
            "VALUES (?, ?, 'qq', 'GroupMessage', 'group_demo', ?, ?, ?, ?, '[\"text\"]', "
            " '2026-09-12 09:00:00')",
            (uid, seq, sender, nickname, 1789000000 + seq, content),
        )
        connection.execute(
            "INSERT INTO chat_memory_message_bots (msg_uid, platform_id, self_id, "
            " conversation_id, umo, role, turn_id, llm_status, persona_id, created_at) "
            "VALUES (?, 'BOT1', '10000', 'conversation_demo', "
            " 'BOT1:GroupMessage:group_demo', ?, ?, 'llm_success', 'persona_demo', "
            " '2026-09-12 09:00:00')",
            (uid, role, turn),
        )
    connection.commit()
    connection.close()


def append_messages(path: Path, rows: list[tuple]) -> None:
    """向演示库追加消息：``(seq, role, sender, nickname, content, turn_id)``。"""
    kinds = json.dumps(["text"], ensure_ascii=False)
    connection = sqlite3.connect(path)
    for seq, role, sender, nickname, content, turn in rows:
        uid = f"p:qq:GroupMessage:group_demo:{seq}"
        connection.execute(
            "INSERT INTO chat_memory_messages (msg_uid, seq, platform_name, "
            " message_type, session_id, sender_id, sender_nickname, "
            " platform_timestamp, content, content_kind, created_at) "
            "VALUES (?, ?, 'qq', 'GroupMessage', 'group_demo', ?, ?, ?, ?, ?, "
            " '2026-09-12 09:00:00')",
            (uid, seq, sender, nickname, 1789000000 + seq, content, kinds),
        )
        connection.execute(
            "INSERT INTO chat_memory_message_bots (msg_uid, platform_id, self_id, "
            " conversation_id, umo, role, turn_id, llm_status, persona_id, created_at) "
            "VALUES (?, 'BOT1', '10000', 'conversation_demo', "
            " 'BOT1:GroupMessage:group_demo', ?, ?, 'llm_success', 'persona_demo', "
            " '2026-09-12 09:00:00')",
            (uid, role, turn),
        )
    connection.commit()
    connection.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cm_pipeline_example_") as temp_dir:
        root = Path(temp_dir)
        cm_db = root / "chat_memory.db"
        own_db = root / "extract.db"
        build_demo_cm(cm_db)

        reply = json.dumps(
            {
                "items": [
                    {
                        "text": "小明约定周六上午十点在老地方见面",
                        "category": "plan",
                        "subject": "qq:10001",
                        "evidence_quote": "周六上午十点",
                        "importance": 0.7,
                    }
                ]
            },
            ensure_ascii=False,
        )
        config = PipelineConfig(
            cm_db=str(cm_db),
            own_db=str(own_db),
            perspective_platform_id="BOT1",
            perspective_self_id="10000",
            extract_threshold=1,
            window_limit=100,
        )
        source = open_cm_source(config.cm_db, dialect=config.dialect)
        sink = SqliteSink(config.own_db)
        extractor = WindowExtractor(
            spec=ExtractionSpec(
                system_prompt="只输出 JSON。",
                user_template="对话：\n{conversation}\n当前日期：{current_date}",
            ),
            client=FakeLlmClient(replies=[reply]),
            attempts=1,
        )
        pipeline = Pipeline(source, extractor, sink, config)
        ticker = AdaptiveTicker(
            pipeline,
            TickerConfig(min_seconds=30, max_seconds=300, initial_seconds=120),
            sleep=lambda _seconds: None,
            rand=lambda: 0.5,  # 关掉抖动，输出稳定
        )
        try:
            print("第一轮（新会话只建基线，不萃取历史）:")
            print(json.dumps(pipeline.scan_once().as_dict(), ensure_ascii=False, indent=2))

            # 基线之后来了新的一轮对话。
            append_messages(
                cm_db,
                [
                    (5, "user", "10001", "小明", "那就周六上午十点，老地方", "turn-3"),
                    (6, "assistant", "10000", "某bot", "好，我记下了", "turn-3"),
                ],
            )
            snapshot = pipeline.backlog_snapshot()
            interval = ticker.next_interval(snapshot, calls_this_tick=0)
            print(
                f"\nticker：待处理 {snapshot.pending_messages} 条 / "
                f"达标 {snapshot.sessions_ready} 个 → 加速到 {interval:.0f}s"
                f"（模式 {ticker.state.mode}）"
            )

            print("\n第二轮（游标后出现新单位，达到阈值 → 萃取）:")
            print(json.dumps(pipeline.scan_once().as_dict(), ensure_ascii=False, indent=2))
            print("\n落库条目:")
            for row in sink.list_items():
                print(
                    f"  [{row['category']}] {row['text']} "
                    f"(证据: {row['evidence_quote']}, 重要性: {row['importance']})"
                )
            print("\n游标状态:", sink.list_cursors())

            snapshot = pipeline.backlog_snapshot()
            interval = ticker.next_interval(snapshot, calls_this_tick=1)
            print(
                f"\nticker：待处理清零 → 降速到 {interval:.0f}s（模式 {ticker.state.mode}）"
            )
        finally:
            source.close()
            sink.close()


if __name__ == "__main__":
    main()
