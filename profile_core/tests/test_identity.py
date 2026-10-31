"""P# 身份映射：同人不同名不拆，异人同名不合。

保留两条方向相反的失配守卫 + 空 sender 不可归属；首次出现顺序已由
``test_render`` 的 ``line_labels`` 断言覆盖，``label_of`` 在生产路径零引用，故删掉那条。
"""

from __future__ import annotations

from profile_core.identity import SpeakerMap, build_speakers

from ._helpers import message


def test_same_sender_with_two_names_is_one_speaker() -> None:
    """守：同一人改名被拆成两个 Speaker——同一个人被记成两份画像。"""
    speakers = build_speakers(
        (
            message(seq=1, sender="10001", name="小明"),
            message(seq=2, sender="10001", name="小明（改名）"),
        )
    )
    assert len(speakers) == 1
    assert speakers[0].label == "P1"
    assert speakers[0].names == ("小明", "小明（改名）")


def test_different_senders_with_same_name_are_two_speakers() -> None:
    """守：同名不同人合并成一个 Speaker——两人的发言被并成一份画像。"""
    speakers = build_speakers(
        (
            message(seq=1, sender="10001", name="小明"),
            message(seq=2, sender="10002", name="小明"),
        )
    )
    assert [speaker.label for speaker in speakers] == ["P1", "P2"]
    assert [speaker.sender_id for speaker in speakers] == ["10001", "10002"]


def test_empty_sender_is_not_labeled() -> None:
    """守：无法归属的发送者被编成 P#——匿名消息被并进某个人的画像。"""
    mapping = SpeakerMap()
    assert mapping.label_for("", "无名") == ""
    assert len(mapping) == 0
