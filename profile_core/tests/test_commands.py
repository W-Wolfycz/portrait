"""指令面：权限必须用装饰器声明，且指令清单不能悄悄漂移。

在函数体里判 ``event.is_admin()`` 时 Dashboard 指令管理仍显示 everyone，
运维会误判谁能用；这里用 AST 静态检查守住这条，不导入 astrbot。
"""

from __future__ import annotations

import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "main.py"

#: 需要管理员权限的指令（子命令名）。
ADMIN_COMMANDS = {"delete", "merge"}
#: 所有人可用的子命令。
PUBLIC_COMMANDS = {"me", "group", "person", "status"}
#: 顶层指令（不在 portrait 组下）。
TOP_LEVEL_COMMANDS = {"画像", "群画像"}


def _decorator_name(node: ast.AST) -> str:
    """把 `@filter.command("x")` / `@portrait_group.command("x")` 归一成 x。"""
    if not isinstance(node, ast.Call):
        return ""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return ""
    if func.attr not in {"command", "command_group"}:
        return ""
    if not node.args:
        return ""
    first = node.args[0]
    return first.value if isinstance(first, ast.Constant) else ""


def _has_admin_decorator(node: ast.AST) -> bool:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        func = decorator.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != "permission_type":
            continue
        for arg in decorator.args:
            if isinstance(arg, ast.Attribute) and arg.attr == "ADMIN":
                return True
    return False


def _functions() -> list[ast.AsyncFunctionDef]:
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    ]


def test_command_permissions_are_declared_by_decorator() -> None:
    """守：函数体里判 is_admin——Dashboard 显示 everyone，与实际行为不符。"""
    offenders = [
        node.name
        for node in _functions()
        if any(
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "is_admin"
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        )
    ]
    assert offenders == [], f"这些指令应在装饰器上声明权限：{offenders}"


def test_admin_commands_carry_admin_permission() -> None:
    """守：管理员指令漏了 permission_type——普通成员能直接改全局画像。"""
    found: dict[str, bool] = {}
    for node in _functions():
        for decorator in node.decorator_list:
            name = _decorator_name(decorator)
            if name in ADMIN_COMMANDS:
                found[name] = _has_admin_decorator(node)
    assert set(found) == ADMIN_COMMANDS, f"管理员指令缺失：{ADMIN_COMMANDS - set(found)}"
    missing = [name for name, ok in found.items() if not ok]
    assert missing == [], f"缺少 ADMIN 权限声明：{missing}"


def test_command_surface_matches_documented_set() -> None:
    """守：指令面悄悄漂移——README 与实际不符，用户按文档用会踩空。"""
    sub_commands: set[str] = set()
    top_level: set[str] = set()
    for node in _functions():
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Attribute) or func.attr != "command":
                continue
            name = _decorator_name(decorator)
            if not name:
                continue
            if isinstance(func.value, ast.Name) and func.value.id == "portrait_group":
                sub_commands.add(name)
            else:
                top_level.add(name)
    assert sub_commands == ADMIN_COMMANDS | PUBLIC_COMMANDS
    assert top_level == TOP_LEVEL_COMMANDS
