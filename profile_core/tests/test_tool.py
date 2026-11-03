"""查询工具：session 绑定、黑名单、目标解析、歧义、未就绪。

删掉的是失败文案（「暂无记录」「画像查询暂时不可用」）与只看返回字符串就发现的
分支；留的是会话归属、越权拦截、默认目标解析、同名消歧这几类无声错。
"""

from __future__ import annotations

import asyncio
import logging

from profile_core.config import Settings
from profile_core.llm import LlmResult
from profile_core.tool import PortraitTool, session_from_event


class FakeLlm:
    def __init__(self, text: str = "answer") -> None:
        self.text = text
        self.prompts: list[str] = []

    async def complete(self, **kwargs) -> LlmResult:
        self.prompts.append(kwargs["prompt"])
        return LlmResult(self.text, {})


class FakeStore:
    def __init__(
        self,
        *,
        group: dict | None = None,
        persons: list[tuple[str, str]] | None = None,
        profiles: dict[str, dict] | None = None,
    ) -> None:
        self.group = group
        self.persons = persons or []
        self.profiles = profiles or {}

    def get_profile_session(self, session):
        return self.group

    def list_profile_persons(self, session):
        return self.persons

    def get_profile_person(self, session, sender_id):
        return self.profiles.get(sender_id)


class FakeRuntime:
    ready = True

    def __init__(self, settings, store, llm) -> None:
        self.settings = settings
        self.store = store
        self.llm = llm


class FakeEvent:
    def __init__(
        self,
        *,
        platform: str = "aiocqhttp",
        message_type: str = "GroupMessage",
        group_id: str = "group_demo",
        sender_id: str = "10001",
    ) -> None:
        self.platform = platform
        self.message_type = message_type
        self.group_id = group_id
        self.sender_id = sender_id

    def get_platform_name(self):
        return self.platform

    def get_message_type(self):
        return self.message_type

    def get_group_id(self):
        return self.group_id

    def get_sender_id(self):
        return self.sender_id


def _tool(settings=None, store=None, llm=None) -> PortraitTool:
    runtime = FakeRuntime(
        settings or Settings(),
        store or FakeStore(),
        llm or FakeLlm(),
    )
    return PortraitTool(runtime, logging.getLogger("test"))


def _ask(tool, event, **kwargs) -> str:
    return asyncio.run(tool.query(event, **kwargs))


def test_session_from_event_group_and_private() -> None:
    """守：私聊/群聊的 session_key 拼错——查到别的会话的画像并沉默返回。"""
    assert session_from_event(FakeEvent()).key == "aiocqhttp|GroupMessage|group_demo"
    private = FakeEvent(message_type="FriendMessage", group_id="", sender_id="10001")
    assert session_from_event(private).key == "aiocqhttp|FriendMessage|10001"
    other = FakeEvent(message_type="OtherMessage", group_id="", sender_id="")
    assert session_from_event(other) is None


def test_group_query_returns_llm_text() -> None:
    """守：群画像 payload / 提问没进 prompt——答案与画像无关且无报错。"""
    llm = FakeLlm("这个群在聊技术")
    tool = _tool(store=FakeStore(group={"theme": "技术"}), llm=llm)
    answer = _ask(tool, FakeEvent(), is_group=True, question="这个群怎么样")
    assert answer == "这个群在聊技术"
    assert "这个群怎么样" in llm.prompts[0]


def test_blacklist_blocks_query() -> None:
    """守：黑名单失效——被排除的会话照样能被查询并暴露画像。"""
    settings = Settings(blacklist_sessions=("G:group_demo",))
    tool = _tool(settings=settings, store=FakeStore(group={"theme": "x"}))
    assert _ask(tool, FakeEvent(), is_group=True) == "该会话未启用画像"


def test_empty_target_defaults_to_current_sender() -> None:
    """守：不传 target 时没落到当前发送者——查成别人的画像。"""
    store = FakeStore(
        persons=[("10001", "小明")],
        profiles={"10001": {"identity": "在杭州"}},
    )
    tool = _tool(store=store)
    assert _ask(tool, FakeEvent(sender_id="10001")) == "answer"


def test_ambiguous_name_returns_candidates() -> None:
    """守：同名时静默选一人——用户拿到的是另一个同名成员的画像。"""
    store = FakeStore(
        persons=[("10001", "小明"), ("10002", "小明")],
        profiles={"10001": {"identity": "a"}, "10002": {"identity": "b"}},
    )
    tool = _tool(store=store)
    answer = _ask(tool, FakeEvent(), target="小明")
    assert "多个" in answer
    assert "QQ" in answer


