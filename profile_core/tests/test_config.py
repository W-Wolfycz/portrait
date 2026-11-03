"""配置：黑名单只接受 F:/G:，不接受 UMO 或裸 id。"""

from __future__ import annotations

from profile_core.config import Settings
from profile_core.contracts import SessionKey


def test_blacklist_accepts_prefixed_rejects_umo() -> None:
    """守：黑名单格式解析漂移——该拦的会话被放行，或误拦正常 id。"""
    group = SessionKey("demo", "GroupMessage", "group_demo")
    friend = SessionKey("demo", "FriendMessage", "10001")
    assert Settings(blacklist_sessions=("G:group_demo",)).is_blacklisted(group)
    assert Settings(blacklist_sessions=("F:10001",)).is_blacklisted(friend)
    assert not Settings(
        blacklist_sessions=("demo|GroupMessage|group_demo",)
    ).is_blacklisted(group)
    assert not Settings(blacklist_sessions=("group_demo",)).is_blacklisted(
        group
    )
