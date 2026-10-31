"""萃取：JSON 契约、逐条校验、证据引用核对、重试与失败语义。"""

from __future__ import annotations

import json
from datetime import datetime

from cm_pipeline.contracts import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SKIP,
    STATUS_SUCCESS,
    Cursor,
    SessionKey,
    Window,
)
from cm_pipeline.extract import ExtractionSpec, WindowExtractor
from cm_pipeline.llm import FakeLlmClient, LlmError

SESSION = SessionKey("qq", "GroupMessage", "group_demo")
WINDOW_TEXT = (
    "<cm_time>2026-09-12 21:30:00</cm_time>"
    '<cm_speaker current="1"/><cm_nickname>小明</cm_nickname> 明天想去打球'
)


def _window() -> Window:
    return Window(session=SESSION, start=Cursor(seq=0), end=Cursor(seq=1), messages=())


def _spec(**overrides) -> ExtractionSpec:
    payload = {
        "system_prompt": "系统提示",
        "user_template": "对话：\n{conversation}\n日期：{current_date}",
        "require_evidence_in_window": True,
    }
    payload.update(overrides)
    return ExtractionSpec(**payload)


def _extractor(replies, **kwargs) -> WindowExtractor:
    client = FakeLlmClient(replies=replies)
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("rand", lambda: 0.0)
    kwargs.setdefault("attempts", 1)
    return WindowExtractor(spec=_spec(), client=client, **kwargs)


def test_valid_payload_produces_items() -> None:
    reply = json.dumps(
        {
            "status": "success",
            "items": [
                {
                    "text": "小明喜欢打球",
                    "category": "preference",
                    "evidence_quote": "明天想去打球",
                    "importance": 0.8,
                    "extra": "保留",
                }
            ],
        },
        ensure_ascii=False,
    )
    extractor = _extractor([reply])
    result = extractor.extract(_window(), WINDOW_TEXT, now=datetime(2026, 9, 12))

    assert result.status == STATUS_SUCCESS
    assert result.is_committable is True
    item = result.items[0]
    assert item.text == "小明喜欢打球"
    assert item.category == "preference"
    assert item.importance == 0.8
    assert item.payload == {"extra": "保留"}
    assert result.prompt_digest
    # 模板占位符被替换。
    _system, user, _timeout = extractor.client.calls[0]
    assert "明天想去打球" in user
    assert "2026-09-12" in user


def test_markdown_fence_and_missing_evidence_are_handled() -> None:
    fenced = "```json\n" + json.dumps(
        {
            "items": [
                {
                    "text": "有证据",
                    "evidence_quote": "明天想去打球",
                },
                {
                    "text": "幻觉条目",
                    "evidence_quote": "窗口里没有这句话",
                },
            ]
        },
        ensure_ascii=False,
    ) + "\n```"
    result = _extractor([fenced]).extract(_window(), WINDOW_TEXT)

    assert result.status == STATUS_SUCCESS
    assert [item.text for item in result.items] == ["有证据"]
    assert len(result.dropped) == 1
    assert "疑似幻觉" in result.dropped[0]


def test_missing_required_field_is_dropped() -> None:
    reply = json.dumps({"items": [{"text": "", "evidence_quote": "明天想去打球"}]})
    result = _extractor([reply]).extract(_window(), WINDOW_TEXT)

    assert result.status == STATUS_EMPTY
    assert result.items == ()
    assert any("缺少必填字段" in reason for reason in result.dropped)


def test_category_allowlist_falls_back() -> None:
    reply = json.dumps(
        {
            "items": [
                {"text": "x", "category": "未知类别", "evidence_quote": "明天想去打球"}
            ]
        },
        ensure_ascii=False,
    )
    extractor = WindowExtractor(
        spec=_spec(category_allowlist=("preference",)),
        client=FakeLlmClient(replies=[reply]),
        sleep=lambda _s: None,
        rand=lambda: 0.0,
        attempts=1,
    )
    result = extractor.extract(_window(), WINDOW_TEXT)

    assert result.items[0].category == "other"


def test_skip_status_short_circuits() -> None:
    reply = json.dumps({"status": "skip", "reason": "内容安全", "items": []}, ensure_ascii=False)
    result = _extractor([reply]).extract(_window(), WINDOW_TEXT)

    assert result.status == STATUS_SKIP
    assert result.skip_reason == "内容安全"
    assert result.is_committable is True


def test_invalid_json_fails_after_attempts() -> None:
    extractor = _extractor(["不是 JSON", "仍然不是"], attempts=2)
    result = extractor.extract(_window(), WINDOW_TEXT)

    assert result.status == STATUS_FAILED
    assert result.is_committable is False
    assert len(extractor.client.calls) == 2


def test_transient_llm_error_is_retried() -> None:
    class FlakyClient(FakeLlmClient):
        """第一次调用抛错，之后返回预设回复。"""

        def __init__(self, replies):
            super().__init__(replies=replies)
            self.fail_first = True

        def complete(self, *, system: str, user: str, timeout: float = 0.0):
            from cm_pipeline.contracts import LlmReply

            self.calls.append((system, user, float(timeout)))
            if self.fail_first:
                self.fail_first = False
                raise LlmError("网络抖动")
            return LlmReply(text=str(self.replies[0]), model=self.model)

    reply = json.dumps(
        {"items": [{"text": "恢复成功", "evidence_quote": "明天想去打球"}]},
        ensure_ascii=False,
    )
    client = FlakyClient(replies=[reply])
    extractor = WindowExtractor(
        spec=_spec(), client=client, attempts=2, sleep=lambda _s: None, rand=lambda: 0.0
    )
    result = extractor.extract(_window(), WINDOW_TEXT)

    assert result.status == STATUS_SUCCESS
    assert result.items[0].text == "恢复成功"
    assert len(client.calls) == 2


def test_max_items_truncates() -> None:
    reply = json.dumps(
        {
            "items": [
                {"text": f"条目 {index}", "evidence_quote": "明天想去打球"}
                for index in range(5)
            ]
        },
        ensure_ascii=False,
    )
    extractor = WindowExtractor(
        spec=_spec(max_items=2),
        client=FakeLlmClient(replies=[reply]),
        attempts=1,
        sleep=lambda _s: None,
        rand=lambda: 0.0,
    )
    result = extractor.extract(_window(), WINDOW_TEXT)

    assert len(result.items) == 2
    assert any("超出 max_items" in reason for reason in result.dropped)
