"""内容安全拒绝识别。"""

from __future__ import annotations

from profile_core.policy import is_policy_refusal


def test_detects_english_policy_refusal() -> None:
    assert is_policy_refusal("The request was blocked by content policy")


def test_detects_chinese_policy_refusal() -> None:
    assert is_policy_refusal("抱歉，这个内容无法协助")


def test_ignores_normal_errors() -> None:
    assert not is_policy_refusal("LLM 超时（provider=demo, timeout=120s）")
