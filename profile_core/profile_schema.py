"""画像结构：长期 / 中期 / 短期三层，以及旧扁平画像的兼容转换。"""

from __future__ import annotations

import logging
import re
from typing import Any

_logger = logging.getLogger(__name__)

TIERS = ("long", "mid", "short")

PERSON_TIERS: dict[str, tuple[str, ...]] = {
    "long": ("identity", "role", "style", "interests", "other"),
    "mid": ("schedule", "relations"),
    "short": ("recent",),
}
GROUP_TIERS: dict[str, tuple[str, ...]] = {
    "long": ("theme", "atmosphere", "norms", "memes"),
    "mid": ("active_hours", "members", "relations", "recurring"),
    "short": ("recent",),
}
PERSON_LIMIT = 80
GROUP_LIMIT = 100
MAX_FIELDS_PER_TIER = 32

_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_WHITESPACE_RE = re.compile(r"\s+")


def clean_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("`", "")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:limit]


def normalize_tiered(
    raw: Any,
    limit: int,
) -> dict[str, dict[str, str]]:
    """校验三层结构：层固定，字段名允许 LLM 新建（snake_case，每层有上限）。"""
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for tier in TIERS:
        source = raw.get(tier)
        if not isinstance(source, dict):
            continue
        valid: list[tuple[str, str]] = []
        for key, value in source.items():
            if not isinstance(key, str) or not _FIELD_RE.match(key):
                continue
            text = clean_text(value, limit)
            if text:
                valid.append((key, text))
        sections = dict(valid[:MAX_FIELDS_PER_TIER])
        if sections:
            result[tier] = sections
        # 只有「合法字段真的超过上限」才告警：非法键名/空值是正常校验，
        # 报出来会误导排查方向，也会把日志刷满。
        if len(valid) > MAX_FIELDS_PER_TIER:
            _logger.warning(
                "画像字段超上限被丢弃 tier=%s kept=%d dropped=%d",
                tier,
                MAX_FIELDS_PER_TIER,
                len(valid) - MAX_FIELDS_PER_TIER,
            )
    return result


def coerce_profile(raw: Any, kind: str) -> dict[str, dict[str, str]]:
    """接受三层结构或旧的扁平结构，统一成三层。"""
    tiers = PERSON_TIERS if kind == "person" else GROUP_TIERS
    limit = PERSON_LIMIT if kind == "person" else GROUP_LIMIT
    if not isinstance(raw, dict):
        return {}
    if any(isinstance(raw.get(tier), dict) for tier in TIERS):
        return normalize_tiered(raw, limit)
    result: dict[str, dict[str, str]] = {}
    for tier, keys in tiers.items():
        sections = {
            key: clean_text(raw.get(key), limit)
            for key in keys
        }
        sections = {key: value for key, value in sections.items() if value}
        if sections:
            result[tier] = sections
    return result
