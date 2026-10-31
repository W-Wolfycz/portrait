"""按批读取：不够阈值时继续下一批。"""

from __future__ import annotations

import logging

from profile_core.config import Settings
from profile_core.contracts import SessionKey
from profile_core.runtime import Runtime

from ._helpers import message

SESSION = SessionKey("demo", "GroupMessage", "group_demo")


class FakeSource:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.calls: list[tuple[int, int]] = []

    def fetch_after(self, session, seq: int, limit: int):
        self.calls.append((int(seq), int(limit)))
        return [row for row in self.rows if row.seq > seq][:limit]


def test_fetch_rows_reads_until_text_threshold() -> None:
    rows = [
        message(seq=1, text="", kinds=("image",)),
        message(seq=2, text="a"),
        message(seq=3, text="b"),
        message(seq=4, text="", kinds=("image",)),
        message(seq=5, text="c"),
        message(seq=6, text="d"),
    ]
    runtime = Runtime(
        None,
        Settings(extract_threshold=3),
        logger=logging.getLogger("test"),
        data_dir=".",
    )
    source = FakeSource(rows)
    runtime.source = source

    assert [item.seq for item in runtime._fetch_rows(SESSION, 0)] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]
    assert source.calls == [(0, 3), (3, 3)]
