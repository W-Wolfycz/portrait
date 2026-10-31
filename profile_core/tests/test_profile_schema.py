"""画像三层结构：旧扁平画像兼容与白名单校验。"""

from __future__ import annotations

from profile_core.profile_schema import coerce_profile


def test_flat_person_profile_is_wrapped_into_tiers() -> None:
    assert coerce_profile(
        {"identity": "在杭州", "schedule": "凌晨", "recent": "在准备考试"},
        "person",
    ) == {
        "long": {"identity": "在杭州"},
        "mid": {"schedule": "凌晨"},
        "short": {"recent": "在准备考试"},
    }


def test_flat_group_profile_is_wrapped_into_tiers() -> None:
    assert coerce_profile(
        {"theme": "技术", "active_hours": "晚上", "recent": "在聊部署"},
        "group",
    ) == {
        "long": {"theme": "技术"},
        "mid": {"active_hours": "晚上"},
        "short": {"recent": "在聊部署"},
    }


def test_custom_fields_are_kept_and_invalid_keys_dropped() -> None:
    assert coerce_profile(
        {
            "long": {
                "identity": "x",
                "custom_note": "y",
                "Bad Key": "drop",
                "9bad": "drop",
            },
            "mid": {"relations": "z"},
            "short": {},
        },
        "person",
    ) == {
        "long": {"identity": "x", "custom_note": "y"},
        "mid": {"relations": "z"},
    }
