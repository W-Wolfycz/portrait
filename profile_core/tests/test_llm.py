"""LLM 适配：provider 解析、超时、空文本、usage。"""

from __future__ import annotations

import asyncio

import pytest

from profile_core.llm import (
    AstrBotLlmClient,
    LlmEmptyError,
    LlmTimeoutError,
)


class FakeUsage:
    input_other = 1
    input_cached = 2
    output = 3


class FakeResponse:
    def __init__(self, text: str, usage=None) -> None:
        self.completion_text = text
        self.usage = usage


class FakeProvider:
    def __init__(self, provider_id: str = "p1") -> None:
        self.provider_config = {"id": provider_id}


class FakeContext:
    def __init__(self, *, text: str = "hello", delay: float = 0.0) -> None:
        self.provider = FakeProvider()
        self.text = text
        self.delay = delay
        self.kwargs: dict = {}

    def get_provider_by_id(self, provider_id: str):
        return self.provider if provider_id == "p1" else None

    async def get_using_provider_async(self):
        return self.provider

    async def llm_generate(self, **kwargs):
        self.kwargs = kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        return FakeResponse(self.text, FakeUsage())


def test_provider_resolution_and_usage() -> None:
    ctx = FakeContext(text="result")
    client = AstrBotLlmClient(ctx, provider_id="")
    result = asyncio.run(
        client.complete(system_prompt="s", prompt="p")
    )
    assert result.text == "result"
    assert ctx.kwargs["chat_provider_id"] == "p1"
    assert result.usage["total"] == 6

    fallback = FakeContext(text="fallback")
    client = AstrBotLlmClient(fallback, provider_id="missing")
    assert asyncio.run(
        client.complete(system_prompt="s", prompt="p")
    ).text == "fallback"


def test_llm_failures_raise_typed_errors() -> None:
    ctx = FakeContext(text="late", delay=0.2)
    client = AstrBotLlmClient(ctx, timeout_seconds=10)
    client._timeout = 0.05
    with pytest.raises(LlmTimeoutError):
        asyncio.run(client.complete(system_prompt="s", prompt="p"))

    empty = FakeContext(text="   ")
    client = AstrBotLlmClient(empty)
    with pytest.raises(LlmEmptyError):
        asyncio.run(client.complete(system_prompt="s", prompt="p"))
