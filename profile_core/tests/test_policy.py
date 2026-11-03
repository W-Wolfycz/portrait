"""内容安全拒绝识别。"""

from __future__ import annotations

from profile_core.policy import is_policy_refusal


def test_is_policy_refusal_cases() -> None:
    """守：拒绝识别漂移——被拒内容被当成普通失败反复重试。"""
    assert is_policy_refusal("The request was blocked by content policy")
    assert is_policy_refusal("抱歉，这个内容无法协助")
    assert not is_policy_refusal("LLM 超时（provider=demo, timeout=120s）")
