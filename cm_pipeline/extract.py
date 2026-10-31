"""窗口萃取：把渲染后的对话交给 LLM，产出经过校验的结构化条目。

职责边界（重要）：

- 本模块只负责「一次窗口 → 一组结构化条目」；**不**决定何时萃取、**不**写库、
  **不**推进游标 —— 那是 :mod:`cm_pipeline.pipeline` 与 :mod:`cm_pipeline.sink` 的事。
- 校验是**逐条**的：单条不合规只丢该条（记入 ``dropped``），不整批失败。
- 只有「JSON 不可解析 / 根结构不对 / 全部重试失败」才算 ``status=failed``，
  失败**不推进游标**，下一次会重试同一窗口。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SKIP,
    STATUS_SUCCESS,
    ExtractionItem,
    ExtractionResult,
    LlmClient,
    Window,
    normalize_quote,
    sha256_hex,
    strip_json_fence,
)
from .llm import LlmError

DEFAULT_SYSTEM_PROMPT = (
    "你是一个中性的信息抽取器。只输出一个标准 JSON 对象，禁止输出 Markdown 之外的任何解释。"
)

_DEFAULT_FIELD_MAP = {
    "text": "text",
    "category": "category",
    "subject": "subject",
    "evidence_quote": "evidence_quote",
    "importance": "importance",
}

_MARKDOWN_RE = re.compile(r"[*_`#>\[\]]+")


@dataclass(frozen=True)
class ExtractionSpec:
    """一套萃取口径：提示词 + 输出契约 + 校验规则。

    宿主按自己的需求替换 ``system_prompt`` / ``user_template`` 即可复用整条链路。
    """

    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    user_template: str = "{conversation}"
    items_key: str = "items"
    status_key: str = "status"
    reason_key: str = "reason"
    max_items: int = 20
    required_fields: tuple[str, ...] = ("text",)
    field_map: Mapping[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_FIELD_MAP)
    )
    category_allowlist: tuple[str, ...] = ()
    category_fallback: str = "other"
    require_evidence_in_window: bool = True
    text_limit: int = 120
    evidence_limit: int = 60
    important_min: float = 0.0
    important_max: float = 1.0

    def prompt_digest(self) -> str:
        return sha256_hex(self.system_prompt, self.user_template, length=16)


def load_prompt(path: str | Path) -> str:
    """读取提示词文件（UTF-8）；找不到时抛 ``FileNotFoundError``。"""
    return Path(path).read_text(encoding="utf-8")


@dataclass
class WindowExtractor:
    """带重试与逐条校验的窗口萃取器。"""

    spec: ExtractionSpec
    client: LlmClient
    attempts: int = 3
    timeout_seconds: float = 120.0
    backoff_seconds: float = 3.0
    sleep: Callable[[float], None] = time.sleep
    rand: Callable[[], float] = random.random

    def extract(
        self,
        window: Window,
        rendered_text: str,
        *,
        now: datetime | None = None,
    ) -> ExtractionResult:
        """执行一次萃取；任何异常都转成 ``status=failed`` 的结果，不向上抛。"""
        current = now or datetime.now()
        user_prompt = (
            self.spec.user_template.replace("{conversation}", rendered_text)
            .replace("{current_date}", current.strftime("%Y-%m-%d %H:%M"))
            .replace("{session_key}", window.session.key)
            .replace("{window_id}", window.window_id)
        )
        digest = self.spec.prompt_digest()
        last_error = ""
        for attempt in range(1, max(1, int(self.attempts)) + 1):
            try:
                reply = self.client.complete(
                    system=self.spec.system_prompt,
                    user=user_prompt,
                    timeout=self.timeout_seconds,
                )
            except LlmError as error:
                last_error = f"LlmError: {error}"
            except Exception as error:  # 客户端实现各异，统一兜住
                last_error = f"{type(error).__name__}: {error}"
            else:
                parsed = self._parse(
                    reply.text, window=window, rendered_text=rendered_text
                )
                if parsed is not None:
                    return ExtractionResult(
                        window=window,
                        status=parsed.status,
                        items=tuple(parsed.items),
                        skip_reason=parsed.skip_reason,
                        dropped=tuple(parsed.dropped),
                        usage=dict(reply.usage or {}),
                        model=reply.model,
                        prompt_digest=digest,
                    )
                # 结构级失败（非 JSON / 根不是对象 / 缺少条目数组）才重试；
                # 单条不合规已在 _parse 内部丢弃，不影响整批。
                last_error = "响应不符合 JSON 契约（非 JSON、根不是对象或缺少条目数组）"
            if attempt < max(1, int(self.attempts)):
                delay = max(0.0, float(self.backoff_seconds)) * attempt
                delay += float(self.rand()) * 0.5
                self.sleep(delay)
        return ExtractionResult(
            window=window,
            status=STATUS_FAILED,
            error=last_error or "未知失败",
            prompt_digest=digest,
        )

    # -- 解析与校验 ------------------------------------------------------

    def _parse(
        self, text: str, *, window: Window, rendered_text: str
    ) -> "_Parsed | None":
        cleaned = strip_json_fence(text or "")
        if not cleaned:
            return None
        try:
            payload = json.loads(cleaned)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None

        status = str(payload.get(self.spec.status_key, STATUS_SUCCESS) or "")
        if status == STATUS_SKIP:
            return _Parsed(
                status=STATUS_SKIP,
                skip_reason=str(payload.get(self.spec.reason_key, "") or "")[:200],
            )
        if status not in ("", STATUS_SUCCESS):
            return None

        raw_items = payload.get(self.spec.items_key)
        if not isinstance(raw_items, list):
            return None

        dropped: list[str] = []
        items: list[ExtractionItem] = []
        haystack = normalize_quote(rendered_text)
        for index, raw in enumerate(raw_items):
            if len(items) >= max(0, int(self.spec.max_items)):
                dropped.append(f"#{index} 超出 max_items={self.spec.max_items}")
                continue
            item, reason = self._build_item(
                raw, index=index, haystack=haystack
            )
            if item is None:
                dropped.append(f"#{index} {reason}")
                continue
            items.append(item)
        final_status = STATUS_SUCCESS if items else STATUS_EMPTY
        return _Parsed(status=final_status, items=items, dropped=dropped)

    def _build_item(
        self, raw: Any, *, index: int, haystack: str
    ) -> tuple[ExtractionItem | None, str]:
        if not isinstance(raw, dict):
            return None, "不是 JSON 对象"
        field_map = self.spec.field_map
        text = _normalize_text(raw.get(field_map.get("text", "text")), self.spec.text_limit)
        if not text:
            return None, f"缺少必填字段 {field_map.get('text', 'text')}"
        for field_name in self.spec.required_fields:
            key = field_map.get(field_name, field_name)
            if not str(raw.get(key, "") or "").strip():
                return None, f"缺少必填字段 {key}"

        evidence = _normalize_text(
            raw.get(field_map.get("evidence_quote", "evidence_quote")),
            self.spec.evidence_limit,
        )
        if self.spec.require_evidence_in_window:
            if not evidence:
                return None, "缺少 evidence_quote"
            if normalize_quote(evidence) not in haystack:
                return None, "evidence_quote 在窗口正文中找不到（疑似幻觉）"

        category = str(raw.get(field_map.get("category", "category"), "") or "").strip()
        if self.spec.category_allowlist and category not in self.spec.category_allowlist:
            category = self.spec.category_fallback

        payload = {
            str(key): value
            for key, value in raw.items()
            if str(key) not in set(field_map.values())
        }
        return (
            ExtractionItem(
                index=index,
                text=text,
                category=category,
                subject_key=str(
                    raw.get(field_map.get("subject", "subject"), "") or ""
                ).strip(),
                evidence_quote=evidence,
                importance=_clamp(
                    raw.get(field_map.get("importance", "importance"), 0.5),
                    self.spec.important_min,
                    self.spec.important_max,
                ),
                payload=payload,
            ),
            "",
        )


@dataclass(frozen=True)
class _Parsed:
    """内部解析产物（与对外 ExtractionResult 区分，便于携带 dropped）。"""

    status: str
    items: Sequence[ExtractionItem] = ()
    skip_reason: str = ""
    dropped: Sequence[str] = ()
    error: str = ""


def _normalize_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = _MARKDOWN_RE.sub("", str(value))
    text = " ".join(text.split())
    if limit > 0 and len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _clamp(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.5
    return max(float(low), min(float(high), number))


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "ExtractionSpec",
    "WindowExtractor",
    "load_prompt",
]
