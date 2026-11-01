"""平台目录映射：群名/群名片/好友备注的纯解析。"""

from __future__ import annotations

from profile_core.directory import _friend_map, _group_map, _member_map


def test_directory_maps_prefer_human_names() -> None:
    assert _group_map(
        {"data": [{"group_id": 10001, "group_name": "测试群"}]}
    ) == {"10001": "测试群"}
    assert _member_map(
        [
            {"user_id": 1, "card": "群名片", "nickname": "昵称"},
            {"user_id": 2, "card": "", "nickname": "昵称二"},
        ]
    ) == {"1": "群名片", "2": "昵称二"}
    assert _friend_map(
        [{"user_id": 3, "remark": "备注", "nickname": "昵称三"}]
    ) == {"3": "备注"}
