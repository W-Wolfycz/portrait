"""模板渲染与 P# 标签：不残留内部 token，不把不同人合并。"""

from __future__ import annotations

from datetime import timezone

from profile_core.render import RenderOptions, render_content, render_segment

from ._helpers import message

UTC = RenderOptions(timezone=timezone.utc)


def test_render_content_token_interop() -> None:
    """守：CM token 映射/隐藏/还原漂移——画像被算错人或内部标记漏进提示词。"""
    msg = message(
        content="⟦CM_AT:0⟧你好",
        relation={"mentions": [{"user_id": "20001", "nickname": "小明"}]},
    )
    text, unrendered = render_content(msg, UTC)
    assert text == "@小明你好"
    assert unrendered == ()

    msg = message(content="前⟦CM_NEW:0⟧后")
    text, unrendered = render_content(msg, UTC)
    assert "⟦CM_NEW:0⟧" not in text
    assert "[未知消息]" in text
    assert unrendered == ("⟦CM_NEW:0⟧",)

    msg = message(content="⦑不是 token⦒")
    text, unrendered = render_content(msg, UTC)
    assert text == "⟦不是 token⟧"
    assert unrendered == ()


def test_same_sender_gets_same_label_across_name_change() -> None:
    """守：同一人改名被拆成两个 P#——同一个人被记成两份画像。"""
    rendered = render_segment(
        (
            message(seq=1, sender="10001", name="小明", text="一"),
            message(seq=2, sender="10001", name="小明（改名）", text="二"),
        ),
        UTC,
    )
    assert rendered.line_labels == ("P1", "P1")
    assert rendered.speakers[0].names == ("小明", "小明（改名）")


def test_different_senders_same_name_get_different_labels() -> None:
    """守：同名不同人合并成一个 P#——两人的发言被并成一份画像。"""
    rendered = render_segment(
        (
            message(seq=1, sender="10001", name="小明", text="一"),
            message(seq=2, sender="10002", name="小明", text="二"),
        ),
        UTC,
    )
    assert rendered.line_labels == ("P1", "P2")


def test_context_message_is_marked() -> None:
    """守：[上下文] 标记丢失——补进来的回复目标被当成新发言，语义无声变形。"""
    msg = message(seq=1, text="被回复的话")
    rendered = render_segment(
        (msg,),
        RenderOptions(timezone=timezone.utc, context_uids=(msg.msg_uid,)),
    )
    assert "[上下文]" in rendered.text


def test_time_is_formatted_in_utc() -> None:
    """守：时间戳按 OS 本地时区格式化——提示词时间整体偏移，作息结论全错。"""
    rendered = render_segment((message(seq=1, ts=0),), UTC)
    assert "2023-11-14 22:13" in rendered.text
    assert rendered.text.startswith("[P1] 2023-11-14 22:13")
