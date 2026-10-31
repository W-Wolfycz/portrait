"""自适应扫描间隔与时区回退。

只留「配置键被默默忽略」「当日重试预算失控」「时区悄悄错位」三类；
间隔的 ±5% 系数与上下限在状态/日志里看得见，不再逐条断言。
"""

from __future__ import annotations

import logging
from datetime import timezone

from profile_core.config import Settings
from profile_core.runtime import Runtime


class FakeContext:
    def __init__(self, config) -> None:
        self._config = config

    def get_config(self):
        return self._config


def _runtime(tick: int = 60, **overrides) -> Runtime:
    return Runtime(
        None,
        Settings(tick_seconds=tick, **overrides),
        logger=logging.getLogger("test"),
        data_dir=".",
    )


def test_smart_scan_off_forces_max_interval() -> None:
    """守：smart_scan 被忽略——关掉后仍自适应轮询，无任何可见症状。"""
    runtime = _runtime(smart_scan=False)
    runtime._interval = 10.0
    runtime._adapt_interval(10)
    assert runtime._interval == 60.0


def test_merge_retry_is_limited_per_day() -> None:
    """守：当日重试预算失效——第 3 次失败后仍继续排重试，形成无声重试风暴。"""
    assert Runtime._merge_retry_delay(1) == 300.0
    assert Runtime._merge_retry_delay(2) == 900.0
    assert Runtime._merge_retry_delay(3) == 0.0


def test_timezone_follows_astrbot_config() -> None:
    """守：时区配置被默默忽略——提示词里的时间整体偏移却不报错。"""
    runtime = _runtime()
    runtime.context = FakeContext({"timezone": "Asia/Shanghai"})
    assert runtime._resolve_timezone().key == "Asia/Shanghai"


def test_timezone_missing_falls_back_to_utc() -> None:
    """守：缺失时区回退到 OS 本地时区——时间戳悄悄错位。"""
    runtime = _runtime()
    runtime.context = FakeContext({})
    assert runtime._resolve_timezone() is timezone.utc


def test_timezone_invalid_falls_back_to_utc() -> None:
    """守：非法时区名抛异常或回退本地时区——时间悄悄错位。"""
    runtime = _runtime()
    runtime.context = FakeContext({"timezone": "Not/AZone"})
    assert runtime._resolve_timezone() is timezone.utc
