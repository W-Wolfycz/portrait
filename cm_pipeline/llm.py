"""LLM 客户端：协议 + 零依赖的 OpenAI 兼容实现 + 测试用假客户端。

设计取舍：

- **本层不做重试**（单次调用），重试、退避与总时长由 :class:`~cm_pipeline.extract.WindowExtractor`
  统一控制，避免「Provider 内部 5 次 × 读超时」与外层重试叠加。
- 只用标准库 ``urllib``，因此整个 ``cm_pipeline`` 不需要 ``httpx`` / ``openai`` / ``requests``。
- 需要其它 Provider（AstrBot Provider、Anthropic、本地模型）时，实现 ``LlmClient.complete``
  即可，不必改动其它模块。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .contracts import LlmClient, LlmReply

DEFAULT_TIMEOUT_SECONDS = 120.0


class LlmError(RuntimeError):
    """LLM 调用失败（网络、HTTP 状态、响应结构异常）。"""

    def __init__(self, message: str, *, status: int = 0) -> None:
        self.status = int(status or 0)
        super().__init__(message)


@dataclass
class HttpOpenAiCompatClient:
    """OpenAI 兼容 ``/chat/completions`` 的最小客户端（标准库实现）。"""

    base_url: str
    api_key: str = ""
    model: str = ""
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    extra_payload: Mapping[str, Any] = field(default_factory=dict)
    temperature: float | None = None
    max_tokens: int = 0
    provider_name: str = "openai_compat"

    def complete(self, *, system: str, user: str, timeout: float = 0.0) -> LlmReply:
        """发起一次非流式对话补全。"""
        if not self.base_url:
            raise LlmError("base_url 为空")
        if not self.model:
            raise LlmError("model 为空")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = float(self.temperature)
        if self.max_tokens > 0:
            payload["max_tokens"] = int(self.max_tokens)
        payload.update(dict(self.extra_payload))

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **dict(self.extra_headers),
        }
        if self.api_key:
            headers.setdefault("Authorization", f"Bearer {self.api_key}")

        request = urllib.request.Request(
            url=f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        effective_timeout = float(timeout or DEFAULT_TIMEOUT_SECONDS)
        try:
            with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:  # pragma: no cover - 需要真实网络
            detail = _safe_error_body(error)
            raise LlmError(
                f"HTTP {error.code} {detail}", status=int(error.code or 0)
            ) from error
        except urllib.error.URLError as error:  # pragma: no cover - 需要真实网络
            raise LlmError(f"网络错误: {error.reason}") from error
        except TimeoutError as error:  # pragma: no cover - 需要真实网络
            raise LlmError(f"请求超时（>{effective_timeout:.0f}s）") from error

        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as error:
            raise LlmError("响应不是合法 JSON") from error
        return _parse_chat_completion(body, fallback_model=self.model)


def _safe_error_body(error: urllib.error.HTTPError) -> str:
    """读取错误响应体，截断后返回（避免把长正文或密钥带进异常串）。"""
    try:
        body = error.read().decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - 错误体不可读
        return ""
    return body[:300]


def _parse_chat_completion(body: Any, *, fallback_model: str) -> LlmReply:
    if not isinstance(body, dict):
        raise LlmError("响应根节点不是对象")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmError("响应缺少 choices")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else None
    text = ""
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # 部分 Provider 返回分段内容；只取 text 段。
            parts = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") in ("text", "output_text")
            ]
            text = "".join(parts)
    if not text and isinstance(first.get("text"), str):
        text = first["text"]
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return LlmReply(
        text=text,
        model=str(body.get("model") or fallback_model),
        usage=dict(usage or {}),
    )


@dataclass
class FakeLlmClient:
    """测试/示例用假客户端：按顺序返回预设文本，或调用 ``responder``。"""

    replies: Sequence[str] = ()
    responder: Callable[[str, str], str] | None = None
    error: BaseException | None = None
    model: str = "fake"
    calls: list[tuple[str, str, float]] = field(default_factory=list)

    def complete(self, *, system: str, user: str, timeout: float = 0.0) -> LlmReply:
        self.calls.append((system, user, float(timeout)))
        if self.error is not None:
            raise self.error
        if self.responder is not None:
            return LlmReply(text=self.responder(system, user), model=self.model)
        index = len(self.calls) - 1
        if index >= len(self.replies):
            raise LlmError("FakeLlmClient 没有更多预设回复")
        return LlmReply(
            text=str(self.replies[index]),
            model=self.model,
            usage={"prompt_tokens": 0, "completion_tokens": 0},
        )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FakeLlmClient",
    "HttpOpenAiCompatClient",
    "LlmClient",
    "LlmError",
]
