"""配置：黑名单只接受 F:/G:，不接受 UMO 或裸 id。"""

from __future__ import annotations

from profile_core.config import Settings
from profile_core.contracts import SessionKey


def test_blacklist_accepts_group_prefix() -> None:
    session = SessionKey("demo", "GroupMessage", "group_demo")
    assert Settings(blacklist_sessions=("G:group_demo",)).is_blacklisted(session)


def test_blacklist_accepts_friend_prefix() -> None:
    session = SessionKey("demo", "FriendMessage", "10001")
    assert Settings(blacklist_sessions=("F:10001",)).is_blacklisted(session)


def test_blacklist_rejects_umo_and_bare_id() -> None:
    session = SessionKey("demo", "GroupMessage", "group_demo")
    assert not Settings(
        blacklist_sessions=("demo|GroupMessage|group_demo",)
    ).is_blacklisted(session)
    assert not Settings(blacklist_sessions=("group_demo",)).is_blacklisted(
        session
    )
