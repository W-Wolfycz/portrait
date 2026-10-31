"""昵称解析：数字 ID、目录精确名、歧义、无匹配、目录故障降级。

删掉的是评分常数（子串 0.8、模糊阈值）与归一化细节——查询结果在界面上看得见；
留的是「解析成错误的人」与「目录故障拖垮查询」这两类无声错。
"""

from __future__ import annotations

import asyncio

from profile_core.nickname import (
    DirectoryNicknameResolver,
    LocalNicknameResolver,
    ResolvedPerson,
)


class FakeDirectory:
    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self.mapping = mapping

    async def aliases(self, session_key: str) -> dict[str, list[str]]:
        return self.mapping


class BrokenDirectory:
    async def aliases(self, session_key: str) -> dict[str, list[str]]:
        raise RuntimeError("api down")


def _run(resolver, text, candidates):
    return asyncio.run(
        resolver.resolve("demo|GroupMessage|group_demo", text, candidates)
    )


def test_numeric_target_hits_sender() -> None:
    """守：纯数字目标被当昵称模糊匹配——查到同名的另一个人，画像张冠李戴。"""
    result = _run(
        LocalNicknameResolver(),
        "10001",
        [("10001", "小明")],
    )
    assert result == [ResolvedPerson("10001", "小明", 1.0)]


def test_directory_exact_name() -> None:
    """守：目录精确名没命中——群里能查到的名字被报成「暂无记录」。"""
    resolver = DirectoryNicknameResolver(
        FakeDirectory({"小明": ["10001"], "小红": ["10002"]})
    )
    result = _run(resolver, "小明", [])
    assert result[0].sender_id == "10001"
    assert result[0].score == 1.0


def test_ambiguous_same_name_returns_all() -> None:
    """守：同名成员被静默收敛成一个——直接展示其中一人的画像。"""
    resolver = DirectoryNicknameResolver(
        FakeDirectory({"小明": ["10001", "10002"]})
    )
    result = _run(resolver, "小明", [])
    assert {item.sender_id for item in result} == {"10001", "10002"}


def test_no_match_returns_empty() -> None:
    """守：无匹配时返回模糊候选——把不相干的人当目标返回给用户。"""
    resolver = DirectoryNicknameResolver(FakeDirectory({"小红": ["10002"]}))
    assert _run(resolver, "不存在", []) == []


def test_directory_failure_falls_back_to_profiles() -> None:
    """守：目录接口异常没兜住——查询整条链路报错，本地画像也用不上。"""
    resolver = DirectoryNicknameResolver(BrokenDirectory())
    result = _run(resolver, "小明", [("10001", "小明")])
    assert result[0].sender_id == "10001"
