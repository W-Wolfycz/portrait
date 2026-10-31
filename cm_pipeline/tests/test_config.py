"""配置：类型转换、校验、JSON 往返。"""

from __future__ import annotations

import json
from pathlib import Path

from cm_pipeline.config import PipelineConfig, load_config, save_config


def test_from_mapping_coerces_types() -> None:
    config = PipelineConfig.from_mapping(
        {
            "extract_threshold": "30",
            "include_kinds": ["text", "image"],
            "llm_timeout_seconds": "60.5",
            "unknown_key": "忽略",
        }
    )

    assert config.extract_threshold == 30
    assert config.include_kinds == ("text", "image")
    assert config.llm_timeout_seconds == 60.5
    assert not hasattr(config, "unknown_key")


def test_validate_reports_problems(tmp_path: Path) -> None:
    config = PipelineConfig(cm_db="", own_db="", dialect="bogus", unit="bogus")
    problems = config.validate()

    assert any("cm_db" in item for item in problems)
    assert any("own_db" in item for item in problems)
    assert any("dialect" in item for item in problems)
    assert any("unit" in item for item in problems)
    assert any("llm_base_url" in item for item in problems)

    cm_db = tmp_path / "cm.db"
    cm_db.write_bytes(b"")
    ok = PipelineConfig(
        cm_db=str(cm_db),
        own_db=str(tmp_path / "own.db"),
        llm_base_url="https://example.invalid/v1",
        llm_model="demo-model",
    )
    assert ok.validate() == []


def test_json_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = PipelineConfig(
        cm_db="/tmp/cm.db",
        own_db="/tmp/own.db",
        include_kinds=("text",),
        protected_user_ids=("10001",),
    )
    save_config(config, path)
    loaded = load_config(path)

    assert loaded == config
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["include_kinds"] == ["text"]
