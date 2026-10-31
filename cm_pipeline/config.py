"""配置：运行参数、JSON 读写、以及从宿主环境推导默认值。

配置刻意保持「纯数据」：一个 dataclass + 一个 JSON 文件，不依赖任何框架。
宿主插件可以把 WebUI 配置映射到这些字段，也可以直接改 JSON。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

from .contracts import LLM_SUCCESS

DEFAULT_TICK_SECONDS = 300.0


@dataclass
class PipelineConfig:
    """运行参数。字段含义见 ``README.md`` 的配置表。"""

    # 数据源与结果库
    cm_db: str = ""
    own_db: str = ""
    dialect: str = "auto"  # auto | shadow | legacy

    # 观察视角（决定 role/persona 取自哪条参与行；留空 = 取首条参与行）
    perspective_platform_id: str = ""
    perspective_self_id: str = ""
    current_user_id: str = ""

    # 过滤与单位
    include_kinds: tuple[str, ...] = ("text",)
    llm_status_filter: tuple[str, ...] = (LLM_SUCCESS,)
    unit: str = "follow_cm"  # follow_cm | message | round

    # 节奏与预算
    tick_seconds: float = DEFAULT_TICK_SECONDS
    tick_min_seconds: float = 30.0
    tick_max_seconds: float = 300.0
    tick_speedup_factor: float = 0.5
    tick_slowdown_factor: float = 1.6
    tick_jitter_ratio: float = 0.1
    tick_budget_exhausted_seconds: float = 900.0
    extract_threshold: int = 100
    window_limit: int = 500
    budget_per_tick: int = 2
    daily_budget: int = 200

    # LLM
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_attempts: int = 3
    llm_timeout_seconds: float = 120.0
    llm_backoff_seconds: float = 3.0

    # 其它
    timezone_name: str = "Asia/Shanghai"
    log_level: str = "INFO"
    protected_user_ids: tuple[str, ...] = field(default_factory=tuple)

    # -- 便捷视图 -------------------------------------------------------

    def ticker_config(self):
        """把 ticker 相关字段组装成 :class:`TickerConfig`（避免上层重复映射）。"""
        from .ticker import TickerConfig

        return TickerConfig(
            min_seconds=self.tick_min_seconds,
            max_seconds=self.tick_max_seconds,
            initial_seconds=self.tick_seconds,
            speedup_factor=self.tick_speedup_factor,
            slowdown_factor=self.tick_slowdown_factor,
            jitter_ratio=self.tick_jitter_ratio,
            budget_exhausted_seconds=self.tick_budget_exhausted_seconds,
        )

    def perspective(self):
        """返回观察视角对象（避免上层直接拼 dataclass）。"""
        from .contracts import Perspective

        return Perspective(
            platform_id=self.perspective_platform_id,
            self_id=self.perspective_self_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "PipelineConfig":
        """从 mapping 构造；忽略未知键，缺失键用默认值。"""
        known = {item.name for item in fields(cls)}
        payload: dict[str, Any] = {}
        for key, value in dict(data or {}).items():
            if key not in known:
                continue
            payload[key] = _coerce(value, getattr(cls(), key, None))
        return cls(**payload)

    def validate(self) -> list[str]:
        """返回问题列表；空列表表示可用。"""
        problems: list[str] = []
        if not self.cm_db:
            problems.append("cm_db 不能为空")
        elif not Path(self.cm_db).exists():
            problems.append(f"cm_db 不存在: {self.cm_db}")
        if not self.own_db:
            problems.append("own_db 不能为空")
        if self.dialect not in ("auto", "shadow", "legacy"):
            problems.append(f"dialect 非法: {self.dialect}")
        if self.unit not in ("follow_cm", "message", "round"):
            problems.append(f"unit 非法: {self.unit}")
        if int(self.extract_threshold) < 1:
            problems.append("extract_threshold 必须 >= 1")
        if int(self.window_limit) < 1:
            problems.append("window_limit 必须 >= 1")
        if int(self.daily_budget) < 0 or int(self.budget_per_tick) < 0:
            problems.append("预算不能为负")
        if float(self.tick_min_seconds) < 1 or float(self.tick_max_seconds) < 1:
            problems.append("tick_min_seconds / tick_max_seconds 必须 >= 1")
        if float(self.tick_min_seconds) > float(self.tick_max_seconds):
            problems.append("tick_min_seconds 不能大于 tick_max_seconds")
        if float(self.tick_budget_exhausted_seconds) < float(self.tick_max_seconds):
            problems.append("tick_budget_exhausted_seconds 不能小于 tick_max_seconds")
        if not self.llm_base_url or not self.llm_model:
            problems.append("llm_base_url / llm_model 不能为空（或由宿主自行注入客户端）")
        return problems


def _coerce(value: Any, sample: Any) -> Any:
    """按默认值的类型做最小转换（JSON 里数组 → tuple，数字 → int/float）。"""
    if isinstance(sample, tuple):
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value)
        if isinstance(value, str):
            return tuple(part for part in value.split(",") if part.strip())
        return ()
    if isinstance(sample, bool):
        return bool(value)
    if isinstance(sample, int) and not isinstance(sample, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return sample
    if isinstance(sample, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return sample
    return value


def load_config(path: str | Path) -> PipelineConfig:
    """从 JSON 文件加载配置（UTF-8）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是 JSON 对象")
    return PipelineConfig.from_mapping(data)


def save_config(config: PipelineConfig, path: str | Path) -> None:
    """写出配置（便于生成模板）。"""
    Path(path).write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "DEFAULT_TICK_SECONDS",
    "PipelineConfig",
    "load_config",
    "save_config",
]
