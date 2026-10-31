"""自适应 ticker：加速/降速/封顶/预算耗尽、模式切换与循环行为。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cm_pipeline.config import PipelineConfig
from cm_pipeline.extract import ExtractionSpec, WindowExtractor
from cm_pipeline.llm import FakeLlmClient
from cm_pipeline.pipeline import Pipeline, TickReport
from cm_pipeline.sink import SqliteSink
from cm_pipeline.source import open_cm_source
from cm_pipeline.ticker import (
    AdaptiveTicker,
    BacklogSnapshot,
    TickerConfig,
    run_forever,
)

from conftest import add_shadow_message


class _StubPipeline:
    """只实现 ticker 需要的三个成员。"""

    def __init__(self, snapshot: BacklogSnapshot, calls_per_tick: int = 0) -> None:
        self.config = SimpleNamespace(budget_per_tick=2)
        self.log = SimpleNamespace(
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        )
        self.snapshot = snapshot
        self.calls_per_tick = calls_per_tick
        self.scans = 0

    def scan_once(self) -> TickReport:
        self.scans += 1
        report = TickReport(started_at="")
        report.extract_calls = self.calls_per_tick
        return report

    def backlog_snapshot(self) -> BacklogSnapshot:
        return self.snapshot


def _ticker(**config_kwargs) -> AdaptiveTicker:
    snapshot = config_kwargs.pop("snapshot", BacklogSnapshot())
    calls = config_kwargs.pop("calls_per_tick", 0)
    config = TickerConfig(**config_kwargs)
    return AdaptiveTicker(
        _StubPipeline(snapshot, calls),  # type: ignore[arg-type]
        config,
        sleep=lambda _s: None,
        rand=lambda: 0.5,  # 抖动为 0
    )


def test_fast_mode_shrinks_to_floor_on_backlog() -> None:
    ticker = _ticker(
        min_seconds=30, max_seconds=300, initial_seconds=300, speedup_factor=0.5
    )
    snapshot = BacklogSnapshot(sessions_with_pending=1, sessions_ready=1, budget_remaining=10)

    first = ticker.next_interval(snapshot, calls_this_tick=1)
    second = ticker.next_interval(snapshot, calls_this_tick=1)
    third = ticker.next_interval(snapshot, calls_this_tick=1)
    fourth = ticker.next_interval(snapshot, calls_this_tick=1)

    assert [first, second, third, fourth] == [150.0, 75.0, 37.5, 30.0]
    assert ticker.state.mode == "fast"


def test_idle_mode_grows_to_ceiling() -> None:
    ticker = _ticker(
        min_seconds=30, max_seconds=300, initial_seconds=30, slowdown_factor=1.6
    )
    snapshot = BacklogSnapshot(budget_remaining=10)  # 没有任何待处理会话

    values = [ticker.next_interval(snapshot, 0) for _ in range(6)]

    assert values[0] == 48.0
    assert values[-1] == 300.0  # 封顶
    assert ticker.state.mode == "idle"


def test_partial_backlog_keeps_interval() -> None:
    ticker = _ticker(min_seconds=30, max_seconds=300, initial_seconds=60)
    snapshot = BacklogSnapshot(
        sessions_with_pending=1, sessions_ready=0, pending_messages=5, budget_remaining=10
    )

    assert ticker.next_interval(snapshot, 0) == 60.0
    assert ticker.state.mode == "normal"


def test_budget_exhausted_uses_long_interval() -> None:
    ticker = _ticker(min_seconds=30, max_seconds=300, budget_exhausted_seconds=900)
    snapshot = BacklogSnapshot(sessions_ready=1, budget_remaining=0)

    interval = ticker.next_interval(snapshot, calls_this_tick=2)

    assert interval == 900.0
    assert ticker.state.mode == "budget_exhausted"


def test_tick_budget_limited_counts_as_backlog() -> None:
    ticker = _ticker(min_seconds=30, max_seconds=300, initial_seconds=120)
    # 快照看起来空闲，但本轮用满每 tick 预算（2）→ 说明还有活儿，应当加速。
    snapshot = BacklogSnapshot(budget_remaining=10)

    interval = ticker.next_interval(snapshot, calls_this_tick=2)

    assert interval == 60.0
    assert ticker.state.mode == "fast"


def test_jitter_is_bounded_by_ratio() -> None:
    ticker = _ticker(min_seconds=100, max_seconds=100, initial_seconds=100, jitter_ratio=0.1)
    snapshot = BacklogSnapshot(budget_remaining=1)
    ticker._rand = lambda: 1.0  # 正向抖动

    assert ticker.next_interval(snapshot, 0) == 110.0

    ticker._rand = lambda: 0.0  # 负向抖动
    assert ticker.next_interval(snapshot, 0) == 90.0


def test_fixed_config_stays_constant() -> None:
    config = TickerConfig.fixed(120)

    assert config.min_seconds == config.max_seconds == 120
    assert config.jitter_ratio == 0.0

    stub = _StubPipeline(BacklogSnapshot(sessions_ready=1, budget_remaining=5))
    ticker = AdaptiveTicker(
        stub,  # type: ignore[arg-type]
        config,
        sleep=lambda _s: None,
        rand=lambda: 0.5,
    )
    snapshot = BacklogSnapshot(sessions_ready=1, budget_remaining=5)

    assert ticker.next_interval(snapshot, 1) == 120.0
    assert ticker.next_interval(snapshot, 1) == 120.0


def test_run_loop_sleeps_between_ticks_and_respects_max_ticks() -> None:
    sleeps: list[float] = []
    stub = _StubPipeline(
        BacklogSnapshot(sessions_ready=1, budget_remaining=10), calls_per_tick=1
    )
    ticker = AdaptiveTicker(
        stub,  # type: ignore[arg-type]
        TickerConfig(min_seconds=30, max_seconds=300, initial_seconds=300),
        sleep=sleeps.append,
        rand=lambda: 0.5,
    )

    ticks = ticker.run(max_ticks=3)

    assert ticks == 3
    assert stub.scans == 3
    assert sleeps == [150.0, 75.0]  # 最后一次不再 sleep
    assert ticker.state.ticks == 3
    assert ticker.state.last_snapshot is not None


def test_run_forever_compat_is_fixed_interval() -> None:
    sleeps: list[float] = []
    stub = _StubPipeline(BacklogSnapshot(budget_remaining=5))
    ticks = run_forever(
        stub,  # type: ignore[arg-type]
        tick_seconds=45,
        sleep=sleeps.append,
        max_ticks=2,
    )

    assert ticks == 2
    assert sleeps == [45.0]


def test_backlog_snapshot_against_real_cm(
    shadow_conn, shadow_cm: Path, tmp_path: Path
) -> None:
    for seq in range(1, 5):  # 4 条消息 = 2 个完整轮
        role = "user" if seq % 2 else "assistant"
        add_shadow_message(
            shadow_conn,
            seq=seq,
            content=f"m{seq}",
            sender_id="10001" if role == "user" else "10000",
            bots=[
                {
                    "platform_id": "BOT1",
                    "self_id": "10000",
                    "role": role,
                    "turn_id": f"turn-{(seq + 1) // 2}",
                }
            ],
        )
    source = open_cm_source(str(shadow_cm))
    sink = SqliteSink(str(tmp_path / "own.db"))
    config = PipelineConfig(
        cm_db=str(shadow_cm),
        own_db=str(tmp_path / "own.db"),
        extract_threshold=2,  # 轮模式 → 4 条消息算达标
        llm_status_filter=("llm_success",),
    )
    pipeline = Pipeline(
        source,
        WindowExtractor(
            spec=ExtractionSpec(), client=FakeLlmClient(replies=[]), attempts=1
        ),
        sink,
        config,
    )
    try:
        before = pipeline.backlog_snapshot()
        assert before.sessions_total == 1
        assert before.sessions_with_pending == 1
        assert before.pending_messages == 4
        assert before.sessions_ready == 1

        pipeline.scan_once()  # 建基线：游标推到最新，积压清零
        after = pipeline.backlog_snapshot()
        assert after.sessions_with_pending == 0
        assert after.pending_messages == 0
        assert after.idle is True

        add_shadow_message(shadow_conn, seq=5, content="新消息")
        partial = pipeline.backlog_snapshot()
        assert partial.sessions_with_pending == 1
        assert partial.pending_messages == 1
        assert partial.sessions_ready == 0  # 未达阈值 → normal，而不是 fast
    finally:
        source.close()
        sink.close()
