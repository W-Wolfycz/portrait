"""cm_pipeline 测试入口：只依赖标准库 + pytest，不导入 AstrBot。

用法::

    python tests/run_tests.py

或直接::

    pytest -q cm_pipeline/tests
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    sys.dont_write_bytecode = True
    # 让 ``import cm_pipeline`` 生效：把包所在的父目录（portrait/）加入 sys.path。
    package_parent = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(package_parent))
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    try:
        import pytest
    except ImportError as exc:  # pragma: no cover - 环境缺失提示
        raise SystemExit(
            "缺少测试依赖：python -m pip install pytest"
        ) from exc

    tests_dir = Path(__file__).resolve().parent
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="cm_pipeline_tests_") as temp_dir:
        try:
            os.chdir(temp_dir)
            return int(
                pytest.main(["-q", "-p", "no:cacheprovider", str(tests_dir)])
            )
        finally:
            os.chdir(original_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
