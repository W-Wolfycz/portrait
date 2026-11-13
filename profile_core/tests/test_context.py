"""画像上下文注入：token 必须还原成可读名字，不能漏 ID。"""

from __future__ import annotations

from profile_core.context import build_portrait_context
from profile_core.contracts import SessionKey
from profile_core.refcodec import make_token

SESSION = SessionKey("aiocqhttp", "GroupMessage", "group_demo")


class _Store:
    def list_profile_persons(self, session: SessionKey):
        return [("10001", "小明")]

    def get_profile_session(self, session: SessionKey):
        return {
            "long": {"relations": f"与{make_token('10001')}关系好"},
            "short": {"recent": "在聊部署"},
        }

    def get_profile_person(self, session: SessionKey, sender_id: str):
        return {"long": {"identity": "学生"}} if sender_id == "10002" else {}

    def field_ages(self, session: SessionKey, subject: str):
        if subject == "group":
            return {("short", "recent"): 40}
        return {}


def test_context_renders_tokens_and_person() -> None:
    text = build_portrait_context(_Store(), SESSION, "10002", "小红")
    assert "「小明」" in text
    assert "{{uid" not in text
    assert "[群画像]" in text
    assert "当前说话者画像" in text
    assert "identity: 学生" in text


def test_context_marks_stale_fields() -> None:
    """守：时效标注丢失后，注入里旧结论看起来和刚确认的一样新。"""
    text = build_portrait_context(_Store(), SESSION, "10002", "小红")
    assert "recent（40 天未更新，可能已过时）" in text
