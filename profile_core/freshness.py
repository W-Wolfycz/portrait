"""字段时效标注：把「距上次内容变化的天数」转成模型可读的提示。

时间信息不进画像 payload，只在渲染/合并时注入，避免污染存储结构。
"""

from __future__ import annotations

#: 这个天数以内不标注，避免每轮都刷一堆「1 天未更新」的噪音。
QUIET_DAYS = 7
#: 超过这个天数按「可能已过时」处理，提示模型可以考虑降级或删除。
STALE_DAYS = 30

AgeMap = dict[tuple[str, str], int]


def age_suffix(days: int) -> str:
    """注入上下文时跟在字段名后面的短标注；未过期返回空串。"""
    if days < QUIET_DAYS:
        return ""
    if days < STALE_DAYS:
        return f"（{days} 天未更新）"
    return f"（{days} 天未更新，可能已过时）"


def merge_block(ages: AgeMap) -> str:
    """给合并提示词用的时效清单；没有记录时返回空串。"""
    if not ages:
        return ""
    lines = ["字段时效（距上次内容变化的天数，未列出的是本轮新增）："]
    for (tier, field), days in sorted(ages.items()):
        if days < QUIET_DAYS:
            continue
        lines.append(f"- {tier}.{field}: {days} 天")
    if len(lines) == 1:
        return ""
    lines.append(
        f"超过 {STALE_DAYS} 天的字段，如果新观察没有再次印证，"
        "可以降级、合并或删除，不要继续当作仍然成立。"
    )
    return "\n".join(lines)
