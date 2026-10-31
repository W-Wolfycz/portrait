"""内容安全/政策拒绝识别：只用于决定是否跳过，不记录正文。"""

from __future__ import annotations

_POLICY_MARKERS = (
    "content_filter",
    "content filter",
    "content policy",
    "policy violation",
    "safety",
    "refusal",
    "refused",
    "blocked",
    "i'm sorry",
    "i cannot",
    "i can't",
    "无法协助",
    "不能协助",
    "拒绝",
    "审核",
    "违规",
    "敏感",
    "政策",
)


def is_policy_refusal(text: str) -> bool:
    """错误/回复文本是否像内容安全拒绝。"""
    haystack = str(text or "").strip().lower()
    if not haystack:
        return False
    return any(marker in haystack for marker in _POLICY_MARKERS)
