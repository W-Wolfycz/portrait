"""自适应 ticker：按积压情况动态调整扫描间隔。

策略（AIMD 风格，规则简单、可预测、可单测）：

.. code-block:: text

    每次扫描后取一次 BacklogSnapshot：
      ├─ 今日预算耗尽           → 间隔 = max(预算耗尽间隔, 上限)
      ├─ 有会话达到可萃取阈值     → 加速：interval = max(下限, interval × speedup)
      │  （或本轮用满每 tick 预算）
      ├─ 只有少量积压（未达阈值）  → 维持当前间隔
      └─ 完全没有积压            → 降速：interval = min(上限, interval × slowdown)

    最后叠加 ±jitter_ratio 的抖动，避免固定节拍。

为什么不用 Hook：旁路分析不需要消息级实时性；轮询让失败只影响自己、
LLM 调用完全可控。自适应只是把「固定 300s」换成「忙时更快、闲时更慢」。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable

from .pipeline import Pipeline, TickReport


@dataclass(frozen=True)
class BacklogSnapshot:
    """一次积压快照（由 :meth:`Pipeline.backlog_snapshot` 产出）。"""

    sessions_total: int = 0
    sessions_with_pending: int = 0
    sessions_ready: int = 0
    pending_messages: int = 0
    budget_remaining: int = 0

    @property
    def idle(self) -> bool:
        return self.sessions_with_pending == 0

    @property
    def has_ready(self) -> bool:
        return self.sessions_ready > 0


@dataclass
class TickerConfig:
    """ticker 参数。``fixed`` 模式把上下限设成同一个值即可退化为固定节拍。"""

    min_seconds: float = 30.0
    max_seconds: float = 300.0
    initial_seconds: float = 60.0
    speedup_factor: float = 0.5
    slowdown_factor: float = 1.6
    jitter_ratio: float = 0.1
    budget_exhausted_seconds: float = 900.0

    @classmethod
    def fixed(cls, seconds: float) -> "TickerConfig":
        value = max(1.0, float(seconds))
        return cls(
            min_seconds=value,
            max_seconds=value,
            initial_seconds=value,
            jitter_ratio=0.0,
        )

    def normalized(self) -> "TickerConfig":
        """把非法值收敛到可用范围（下限 ≤ 初值 ≤ 上限）。"""
        low = max(1.0, float(self.min_seconds))
        high = max(low, float(self.max_seconds))
        initial = min(max(float(self.initial_seconds), low), high)
        return TickerConfig(
            min_seconds=low,
            max_seconds=high,
            initial_seconds=initial,
            speedup_factor=min(max(float(self.speedup_factor), 0.05), 0.99),
            slowdown_factor=max(1.01, float(self.slowdown_factor)),
            jitter_ratio=min(max(float(self.jitter_ratio), 0.0), 0.5),
            budget_exhausted_seconds=max(
                high, float(self.budget_exhausted_seconds)
            ),
        )


@dataclass
class TickerState:
    """可观测状态（供状态查询与测试断言）。"""

    interval: float = 0.0
    mode: str = "idle"  # fast | normal | idle | budget_exhausted
    ticks: int = 0
    last_snapshot: BacklogSnapshot | None = None
    history: list[float] = field(default_factory=list)


class AdaptiveTicker:
    """按积压驱动的扫描节拍控制器。"""

    def __init__(
        self,
        pipeline: Pipeline,
        config: TickerConfig | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
        logger: logging.Logger | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.config = (config or TickerConfig()).normalized()
        self._sleep = sleep
        self._rand = rand
        self.log = logger or getattr(pipeline, "log", logging.getLogger("cm_pipeline"))
        self.state = TickerState(interval=self.config.initial_seconds)

    # -- 决策 -----------------------------------------------------------

    def next_interval(
        self, snapshot: BacklogSnapshot, calls_this_tick: int
    ) -> float:
        """根据积压快照决定下一次扫描前要等多久（含抖动）。"""
        base = self.state.interval
        mode: str
        if snapshot.budget_remaining <= 0:
            mode = "budget_exhausted"
            base = self.config.budget_exhausted_seconds
        elif snapshot.has_ready or self._budget_limited(calls_this_tick):
            mode = "fast"
            base = max(self.config.min_seconds, base * self.config.speedup_factor)
        elif not snapshot.idle:
            mode = "normal"
            base = min(max(base, self.config.min_seconds), self.config.max_seconds)
        else:
            mode = "idle"
            base = min(self.config.max_seconds, base * self.config.slowdown_factor)

        base = min(max(base, self.config.min_seconds), self.config.max_seconds)
        if mode == "budget_exhausted":
            base = self.config.budget_exhausted_seconds

        jitter = base * self.config.jitter_ratio * (self._rand() * 2.0 - 1.0)
        interval = max(1.0, base + jitter)
        if mode != self.state.mode:
            self.log.info(
                "[ticker] 模式切换 %s → %s（待处理会话=%d，达标=%d，待处理消息=%d，"
                "本轮调用=%d，间隔=%.0fs）",
                self.state.mode,
                mode,
                snapshot.sessions_with_pending,
                snapshot.sessions_ready,
                snapshot.pending_messages,
                calls_this_tick,
                interval,
            )
        self.state.mode = mode
        self.state.interval = base
        self.state.history.append(round(interval, 2))
        if len(self.state.history) > 200:
            del self.state.history[:100]
        return interval

    def _budget_limited(self, calls_this_tick: int) -> bool:
        """本轮用满每 tick 预算 = 还有活儿没干完，应当加速。"""
        per_tick = int(getattr(self.pipeline.config, "budget_per_tick", 0) or 0)
        return per_tick > 0 and calls_this_tick >= per_tick

    # -- 循环 -----------------------------------------------------------

    def run(self, *, max_ticks: int = 0) -> int:
        """常驻循环：``max_ticks=0`` 表示不限次。返回实际执行的 tick 数。"""
        ticks = 0
        while True:
            report = self._safe_scan()
            snapshot = self.pipeline.backlog_snapshot()
            self.state.last_snapshot = snapshot
            interval = self.next_interval(snapshot, report.extract_calls)
            self.log.debug(
                "[ticker] tick=%d 调用=%d 待处理=%d 达标=%d 间隔=%.0fs 模式=%s",
                ticks + 1,
                report.extract_calls,
                snapshot.sessions_with_pending,
                snapshot.sessions_ready,
                interval,
                self.state.mode,
            )
            ticks += 1
            self.state.ticks = ticks
            if max_ticks and ticks >= max_ticks:
                return ticks
            self._sleep(interval)

    def _safe_scan(self) -> TickReport:
        try:
            return self.pipeline.scan_once()
        except Exception as error:  # 单轮失败不终止守护进程
            self.log.warning(
                "[ticker] 本轮扫描失败: %s: %s", type(error).__name__, error
            )
            return TickReport(started_at="")


def run_forever(
    pipeline: Pipeline,
    *,
    tick_seconds: float = 300.0,
    sleep: Callable[[float], None] = time.sleep,
    max_ticks: int = 0,
) -> int:
    """固定节拍的兼容入口（等价于 ``TickerConfig.fixed``）。"""
    ticker = AdaptiveTicker(
        pipeline, TickerConfig.fixed(tick_seconds), sleep=sleep
    )
    return ticker.run(max_ticks=max_ticks)


__all__ = [
    "AdaptiveTicker",
    "BacklogSnapshot",
    "TickerConfig",
    "TickerState",
    "run_forever",
]
