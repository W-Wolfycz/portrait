"""自适应扫描间隔与时区回退。"""

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


def test_timezone_resolution() -> None:
    """守：时区配置被忽略或非法值回落 OS 本地——提示词时间整体偏移。"""
    runtime = _runtime()
    runtime.context = FakeContext({"timezone": "Asia/Shanghai"})
    assert runtime._resolve_timezone().key == "Asia/Shanghai"

    runtime.context = FakeContext({})
    assert runtime._resolve_timezone() is timezone.utc

    runtime.context = FakeContext({"timezone": "Not/AZone"})
    assert runtime._resolve_timezone() is timezone.utc
