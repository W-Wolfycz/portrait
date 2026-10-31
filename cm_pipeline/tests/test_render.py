"""渲染：身份标签、转义、内容过滤与「单位」计数。"""

from __future__ import annotations

from cm_pipeline.contracts import (
    LLM_SUCCESS,
    CmMessage,
    Cursor,
    Participant,
    Perspective,
    SessionKey,
    Window,
)
from cm_pipeline.render import RenderOptions, count_units, render_window

SESSION = SessionKey("qq", "GroupMessage", "group_demo")


def _message(
    *,
    seq: int,
    sender_id: str = "10001",
    nickname: str = "Alice",
    content: str = "明天想去打球",
    kinds: tuple[str, ...] = ("text",),
    role: str = "user",
    self_id: str = "10000",
    turn_id: str = "turn-1",
    llm_status: str = LLM_SUCCESS,
    timestamp: int = 1789000000,
) -> CmMessage:
    return CmMessage(
        msg_uid=f"u{seq}",
        session=SESSION,
        seq=seq,
        sender_id=sender_id,
        sender_nickname=nickname,
        content=content,
        content_kinds=kinds,
        platform_timestamp=timestamp,
        recalled=False,
        deleted=False,
        participants=(
            Participant(
                platform_id="BOT1",
                self_id=self_id,
                role=role,
                turn_id=turn_id,
                llm_status=llm_status,
                persona_id="persona_demo",
            ),
        ),
        raw={},
    )


def _window(*messages: CmMessage) -> Window:
    return Window(
        session=SESSION,
        start=Cursor(seq=messages[0].seq - 1),
        end=Cursor(seq=messages[-1].seq),
        messages=messages,
    )


def test_identity_tags_for_user_self_bot_other_bot_and_unknown() -> None:
    perspective = Perspective(platform_id="BOT1", self_id="10000")
    options = RenderOptions(perspective=perspective, current_user_id="10001")

    user = _message(seq=1, sender_id="10001", role="user")
    self_bot = _message(seq=2, sender_id="10000", nickname="某bot", role="assistant")
    other_bot = CmMessage(
        msg_uid="u3",
        session=SESSION,
        seq=3,
        sender_id="20000",
        sender_nickname="别的bot",
        content="其它实例的发言",
        content_kinds=("text",),
        platform_timestamp=1789000000,
        recalled=False,
        deleted=False,
        participants=(
            Participant(platform_id="BOT2", self_id="20000", role="assistant"),
        ),
        raw={},
    )
    unknown = CmMessage(
        msg_uid="u4",
        session=SESSION,
        seq=4,
        sender_id="10002",
        sender_nickname="",
        content="没有参与行",
        content_kinds=("text",),
        platform_timestamp=1789000000,
        recalled=False,
        deleted=False,
        participants=(),
        raw={},
    )

    text = render_window(
        _window(user, self_bot, other_bot, unknown), options
    ).text
    lines = text.split("\n")

    assert '<cm_speaker current="1"/>' in lines[0]
    assert "<cm_nickname>Alice</cm_nickname>" in lines[0]
    assert '<cm_speaker bot="1"/>' in lines[1]
    assert "<cm_bot/>" not in lines[1]  # 自己的发言不标「其它 bot」
    assert "<cm_bot/>" in lines[2]  # 其它实例的发言如实标出
    assert "<cm_speaker" not in lines[3]  # 无参与行 → 不猜角色
    assert "<cm_nickname>?</cm_nickname>" in lines[3]  # 昵称缺失显示 ?


def test_render_never_leaks_account_id() -> None:
    options = RenderOptions(
        perspective=Perspective(platform_id="BOT1", self_id="10000"),
        current_user_id="10001",
    )
    text = render_window(_window(_message(seq=1)), options).text

    assert "10001" not in text
    assert "10000" not in text


def test_render_escapes_nickname_and_truncates_content() -> None:
    options = RenderOptions(max_chars_per_message=10)
    message = _message(seq=1, nickname="A&B<C>", content="一二三四五六七八九十十一")
    line = render_window(_window(message), options).text

    assert "<cm_nickname>A&amp;B&lt;C&gt;</cm_nickname>" in line
    assert line.endswith("一二三四五六七八九…")


def test_render_filters_non_text_kinds() -> None:
    options = RenderOptions(include_kinds=("text",))
    text_message = _message(seq=1, content="文本")
    image_message = _message(seq=2, content="[图片]", kinds=("image",))
    result = render_window(_window(text_message, image_message), options)

    assert result.message_count == 1
    assert "文本" in result.text
    assert "[图片]" not in result.text


def test_count_units_by_message_and_round() -> None:
    user = _message(seq=1, role="user", turn_id="turn-1")
    assistant = _message(seq=2, role="assistant", turn_id="turn-1")
    user2 = _message(seq=3, role="user", turn_id="turn-2")

    assert count_units([user, assistant, user2], unit="message") == 3
    assert count_units([user, assistant, user2], unit="round") == 1


def test_count_units_follow_cm_semantics() -> None:
    user = _message(seq=1, role="user", turn_id="turn-1")
    assistant = _message(seq=2, role="assistant", turn_id="turn-1")
    pending = _message(
        seq=3, role="user", turn_id="turn-2", llm_status="llm_pending"
    )

    # 仅 llm_success → 按完整轮；轮内必须同时有 user 与 assistant。
    assert count_units([user, assistant, pending], unit="follow_cm") == 1
    # 含其他状态 → 按过滤后的消息条数（三状态都在白名单里）。
    assert (
        count_units(
            [user, assistant, pending],
            unit="follow_cm",
            llm_status_filter=("llm_success", "llm_pending"),
        )
        == 3
    )


def test_count_units_falls_back_when_turn_id_missing() -> None:
    first = _message(seq=1, role="user", turn_id="")
    second = _message(seq=2, role="assistant", turn_id="")

    assert count_units([first, second], unit="round") == 2
