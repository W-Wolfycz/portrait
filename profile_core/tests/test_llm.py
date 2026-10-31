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


def test_uses_default_provider_when_unset() -> None:
    ctx = FakeContext(text="result")
    client = AstrBotLlmClient(ctx, provider_id="")
    result = asyncio.run(
        client.complete(system_prompt="s", prompt="p")
    )
    assert result.text == "result"
    assert ctx.kwargs["chat_provider_id"] == "p1"
    assert result.usage["total"] == 6


def test_missing_configured_provider_falls_back() -> None:
    ctx = FakeContext(text="fallback")
    client = AstrBotLlmClient(ctx, provider_id="missing")
    assert asyncio.run(
        client.complete(system_prompt="s", prompt="p")
    ).text == "fallback"


def test_timeout_raises() -> None:
    ctx = FakeContext(text="late", delay=0.2)
    client = AstrBotLlmClient(ctx, timeout_seconds=10)
    client._timeout = 0.05
    with pytest.raises(LlmTimeoutError):
        asyncio.run(client.complete(system_prompt="s", prompt="p"))


def test_empty_text_raises() -> None:
    ctx = FakeContext(text="   ")
    client = AstrBotLlmClient(ctx)
    with pytest.raises(LlmEmptyError):
        asyncio.run(client.complete(system_prompt="s", prompt="p"))
