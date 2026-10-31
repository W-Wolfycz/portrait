"""LLM 适配：唯一 provider，供萃取/合并/tool 共用。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol


class LlmError(RuntimeError):
    """LLM 调用失败（不包含 prompt 正文）。"""


class LlmTimeoutError(LlmError):
    pass


class LlmEmptyError(LlmError):
    pass


@dataclass(frozen=True)
class LlmResult:
    text: str
    usage: dict[str, Any] = field(default_factory=dict)


class LlmClient(Protocol):
    async def complete(
        self,
        *,
        system_prompt: str,
        prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LlmResult:
        ...


class AstrBotLlmClient:
    """通过 AstrBot ``Context.llm_generate`` 调用指定/默认 provider。"""

    def __init__(
        self,
        context: Any,
        *,
        provider_id: str = "",
        timeout_seconds: int = 120,
        max_concurrency: int = 1,
        logger: Any = None,
    ) -> None:
        self._context = context
        self._configured_id = (provider_id or "").strip()
        self._timeout = max(10, int(timeout_seconds))
        self._logger = logger
        self._resolved_id = ""
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def _resolve_provider_id(self) -> str:
        async with self._lock:
            if self._resolved_id:
                return self._resolved_id
            if self._configured_id:
                provider = self._context.get_provider_by_id(self._configured_id)
                if provider is not None:
                    self._resolved_id = self._configured_id
                    return self._resolved_id
                if self._logger:
                    self._logger.warning(
                        "[Portrait] 配置的 provider 不存在，改用默认 provider"
                    )
            provider = await self._context.get_using_provider_async()
            if provider is None:
                raise LlmError("没有可用的聊天 provider")
            config = getattr(provider, "provider_config", {}) or {}
            self._resolved_id = str(config.get("id") or "default")
            return self._resolved_id

    async def complete(
        self,
        *,
        system_prompt: str,
        prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LlmResult:
        async with self._semaphore:
            return await self._complete_locked(
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )

    async def _complete_locked(
        self,
        *,
        system_prompt: str,
        prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LlmResult:
        provider_id = await self._resolve_provider_id()
        try:
            response = await asyncio.wait_for(
                self._context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            raise LlmTimeoutError(
                f"LLM 超时（provider={provider_id}, timeout={self._timeout}s）"
            ) from exc
        except LlmError:
            raise
        except Exception as exc:
            raise LlmError(
                f"LLM 调用失败（provider={provider_id}）：{type(exc).__name__}"
            ) from exc

        text = (response.completion_text or "").strip()
        if not text:
            raise LlmEmptyError("LLM 返回空文本")
        return LlmResult(text=text, usage=_usage_dict(response))


def _usage_dict(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return {str(key): value for key, value in usage.items()}
    input_other = int(getattr(usage, "input_other", 0) or 0)
    input_cached = int(getattr(usage, "input_cached", 0) or 0)
    output = int(getattr(usage, "output", 0) or 0)
    return {
        "input": input_other + input_cached,
        "input_cached": input_cached,
        "output": output,
        "total": input_other + input_cached + output,
    }
