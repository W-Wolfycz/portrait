"""可选适配层（宿主相关）。

核心 ``cm_pipeline`` 不 import 本目录；把包搬走时可直接删除 ``optional/``。
"""

from .astrbot_env import (
    HostPaths,
    ProviderInfo,
    discover_data_dir,
    list_providers,
    locate_paths,
    pick_provider,
    read_cmd_config,
)

__all__ = [
    "HostPaths",
    "ProviderInfo",
    "discover_data_dir",
    "list_providers",
    "locate_paths",
    "pick_provider",
    "read_cmd_config",
]
