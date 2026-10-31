"""可选适配层：定位 AstrBot 的部署路径、读取其配置文件（不 import astrbot）。

单独放在 ``cm_pipeline/optional/`` 是为了让核心保持「宿主无关」：
如果你把 ``cm_pipeline`` 整包搬到别的插件里，可以把本目录删掉，核心一样能用。

这里只做两件事：

1. 从 AstrBot 数据目录里找到 CM 库、persona 库、``cmd_config.json``；
2. 从 ``cmd_config.json`` 里解析出一个 OpenAI 兼容的 provider（base_url / api_key / model）。

注意：``cmd_config.json`` 带 UTF-8 BOM，必须用 ``utf-8-sig`` 读。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DATA_DIR_CANDIDATES = tuple(
    candidate
    for candidate in (
        # Windows：AstrBot 默认把数据放在用户目录下（本机即 C:\Users\<user>\.astrbot\data）
        str(Path.home() / ".astrbot" / "data"),
        # 兼容：数据目录挪到别处时用环境变量指定（AstrBot 自身也认这个变量）
        str(Path(os.environ["ASTRBOT_ROOT"]) / "data") if os.environ.get("ASTRBOT_ROOT") else "",
    )
    if candidate
)

CM_DB_RELATIVE = ("plugin_data/chat_memory/chat_memory.db", "chat_memory.db")
PERSONA_DB_RELATIVE = ("data_v4.db",)
CMD_CONFIG_RELATIVE = ("cmd_config.json",)


@dataclass(frozen=True)
class HostPaths:
    """AstrBot 数据目录里的关键路径（缺失为 ``None``）。"""

    data_dir: Path
    cm_db: Path | None
    persona_db: Path | None
    cmd_config: Path | None


@dataclass(frozen=True)
class ProviderInfo:
    """从 ``cmd_config.json`` 解析出的 provider 概要。"""

    provider_id: str
    provider_type: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 0.0


def looks_like_data_dir(path: Path) -> bool:
    """判断目录是否像 AstrBot 数据目录。"""
    return (path / "cmd_config.json").exists() or (path / "plugin_data").is_dir()


def discover_data_dir(candidates: Iterable[str] | None = None) -> Path | None:
    """按候选顺序找到第一个像样的 AstrBot 数据目录。"""
    for raw in candidates or DEFAULT_DATA_DIR_CANDIDATES:
        path = Path(raw)
        if path.is_dir() and looks_like_data_dir(path):
            return path
    return None


def locate_paths(data_dir: str | Path) -> HostPaths:
    """在给定数据目录里定位 CM 库、persona 库与配置文件。"""
    root = Path(data_dir)

    def _first(relatives: Iterable[str]) -> Path | None:
        for relative in relatives:
            candidate = root / relative
            if candidate.exists():
                return candidate
        return None

    return HostPaths(
        data_dir=root,
        cm_db=_first(CM_DB_RELATIVE),
        persona_db=_first(PERSONA_DB_RELATIVE),
        cmd_config=_first(CMD_CONFIG_RELATIVE),
    )


def read_cmd_config(path: str | Path) -> dict[str, Any]:
    """读取 ``cmd_config.json``（自动处理 BOM）。"""
    text = Path(path).read_text(encoding="utf-8-sig")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("cmd_config.json 根节点不是对象")
    return data


def list_providers(cmd_config: dict[str, Any]) -> list[ProviderInfo]:
    """列出配置中的 chat provider（含 provider_sources 引用）。"""
    sources = {
        str(item.get("id") or ""): item
        for item in _as_list(cmd_config.get("provider_sources"))
    }
    result: list[ProviderInfo] = []
    for index, item in enumerate(_as_list(cmd_config.get("provider"))):
        merged = dict(item)
        source_id = str(item.get("provider_source_id") or "")
        if source_id and source_id in sources:
            merged = {**sources[source_id], **merged}
        info = _to_provider_info(merged, fallback_id=f"provider_{index}")
        if info is not None:
            result.append(info)
    return result


def pick_provider(
    cmd_config: dict[str, Any], provider_id: str = ""
) -> ProviderInfo | None:
    """按 id 选 provider；``provider_id`` 为空时取第一个可用的。"""
    providers = list_providers(cmd_config)
    if provider_id:
        for info in providers:
            if info.provider_id == provider_id:
                return info
        return None
    return providers[0] if providers else None


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _to_provider_info(item: dict[str, Any], *, fallback_id: str) -> ProviderInfo | None:
    keys = item.get("key") if isinstance(item.get("key"), list) else []
    api_key = str(keys[0]) if keys else str(item.get("api_key") or "")
    base_url = str(item.get("api_base") or item.get("base_url") or "")
    model = str(item.get("model") or item.get("model_config", {}).get("model") or "")
    if not base_url or not model:
        return None
    try:
        timeout = float(item.get("timeout") or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    return ProviderInfo(
        provider_id=str(item.get("id") or fallback_id),
        provider_type=str(item.get("type") or ""),
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout,
    )


__all__ = [
    "CMD_CONFIG_RELATIVE",
    "CM_DB_RELATIVE",
    "DEFAULT_DATA_DIR_CANDIDATES",
    "PERSONA_DB_RELATIVE",
    "HostPaths",
    "ProviderInfo",
    "discover_data_dir",
    "list_providers",
    "locate_paths",
    "looks_like_data_dir",
    "pick_provider",
    "read_cmd_config",
]
