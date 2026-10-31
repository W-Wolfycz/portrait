"""可选适配层：路径发现与 provider 解析（不 import astrbot）。"""

from __future__ import annotations

import json
from pathlib import Path

from cm_pipeline.optional.astrbot_env import (
    discover_data_dir,
    list_providers,
    locate_paths,
    pick_provider,
    read_cmd_config,
)

CMD_CONFIG = {
    "provider_sources": [
        {
            "id": "src_demo",
            "type": "openai_chat_completion",
            "api_base": "https://example.invalid/v1",
            "key": ["sk-demo"],
        }
    ],
    "provider": [
        {
            "id": "demo_chat",
            "provider_source_id": "src_demo",
            "model": "demo-model",
            "timeout": 90,
        },
        {"id": "incomplete", "model": ""},
    ],
}


def test_read_cmd_config_handles_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "cmd_config.json"
    path.write_text("\ufeff" + json.dumps(CMD_CONFIG), encoding="utf-8")

    data = read_cmd_config(path)

    assert "provider" in data


def test_list_and_pick_providers(tmp_path: Path) -> None:
    path = tmp_path / "cmd_config.json"
    path.write_text(json.dumps(CMD_CONFIG), encoding="utf-8")
    data = read_cmd_config(path)

    providers = list_providers(data)

    assert [item.provider_id for item in providers] == ["demo_chat"]  # 缺 model 的被跳过
    info = pick_provider(data, "demo_chat")
    assert info is not None
    assert info.base_url == "https://example.invalid/v1"
    assert info.api_key == "sk-demo"
    assert info.model == "demo-model"
    assert info.timeout_seconds == 90.0
    assert pick_provider(data, "不存在") is None
    # 留空时取第一个可用 provider。
    assert pick_provider(data, "").provider_id == "demo_chat"


def test_locate_paths_and_discover_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    plugin_data = data_dir / "plugin_data" / "chat_memory"
    plugin_data.mkdir(parents=True)
    (plugin_data / "chat_memory.db").write_bytes(b"")
    (data_dir / "data_v4.db").write_bytes(b"")
    (data_dir / "cmd_config.json").write_text("{}", encoding="utf-8")

    paths = locate_paths(data_dir)

    assert paths.cm_db == plugin_data / "chat_memory.db"
    assert paths.persona_db == data_dir / "data_v4.db"
    assert paths.cmd_config == data_dir / "cmd_config.json"

    assert discover_data_dir([str(tmp_path / "missing"), str(data_dir)]) == data_dir
    assert discover_data_dir([str(tmp_path / "missing")]) is None
