"""画像人物引用：DB 存 token，展示/提示词渲染成昵称。

存储格式：
    {{uid:123456}}

渲染给模型：
    「小明」
    「小明#1」   同名消歧

渲染给用户：
    小明         不带 #，头像/QQ 号负责区分
"""

from __future__ import annotations

import re
from typing import Any, Iterable

TOKEN_RE = re.compile(r"\{\{uid:([^{}]+)\}\}")
LABEL_RE = re.compile(r"「([^」]+)」")
P_REF_RE = re.compile(r"(?<![A-Za-z0-9_])P(\d+)(?![A-Za-z0-9_])")
UNKNOWN_NAME = "未命名"


def make_token(sender_id: str) -> str:
    return "{{uid:" + str(sender_id).strip() + "}}"


def _clean_name(name: str) -> str:
    text = str(name or "").strip()
    return text or UNKNOWN_NAME


class RefCatalog:
    """一次读取/合并请求内的稳定人物目录。"""

    def __init__(self, entries: Iterable[tuple[str, str]]) -> None:
        names: dict[str, str] = {}
        for sender_id, name in entries:
            key = str(sender_id or "").strip()
            if key:
                names[key] = _clean_name(name)

        grouped: dict[str, list[str]] = {}
        for sender_id, name in names.items():
            grouped.setdefault(name.casefold(), []).append(sender_id)

        self._names = names
        self._labels: dict[str, str] = {}
        self._label_to_id: dict[str, str] = {}
        self._plain_name_to_id: dict[str, str] = {}
        for same_name in grouped.values():
            same_name = sorted(same_name)
            for index, sender_id in enumerate(same_name, 1):
                base = names[sender_id]
                label = base if len(same_name) == 1 else f"{base}#{index}"
                self._labels[sender_id] = label
                self._label_to_id[label] = sender_id
                if len(same_name) == 1:
                    self._label_to_id[base] = sender_id
                    if (
                        base != UNKNOWN_NAME
                        and not base.isdigit()
                        and len(base) >= 2
                        and "#" not in base
                    ):
                        self._plain_name_to_id[base] = sender_id

    def label(self, sender_id: str) -> str:
        key = str(sender_id or "").strip()
        return self._labels.get(key) or self._names.get(key) or UNKNOWN_NAME

    def name(self, sender_id: str) -> str:
        return self._names.get(str(sender_id or "").strip()) or UNKNOWN_NAME

    def resolve(self, label: str) -> str | None:
        text = str(label or "").strip()
        if not text:
            return None
        if text in self._label_to_id:
            return self._label_to_id[text]
        # 允许模型漏写 #n；只有唯一的名字才能反解，不猜。
        matches = [
            sender_id
            for candidate, sender_id in self._label_to_id.items()
            if candidate.split("#", 1)[0] == text
        ]
        return matches[0] if len(matches) == 1 else None

    def render(self, text: str, *, for_llm: bool) -> str:
        def replace(match: re.Match[str]) -> str:
            sender_id = match.group(1)
            if for_llm:
                return "「" + self.label(sender_id) + "」"
            return self.name(sender_id)

        return TOKEN_RE.sub(replace, text)

    def render_value(self, value: Any, *, for_llm: bool) -> Any:
        if isinstance(value, str):
            return self.render(value, for_llm=for_llm)
        if isinstance(value, dict):
            return {
                key: self.render_value(item, for_llm=for_llm)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                self.render_value(item, for_llm=for_llm) for item in value
            ]
        return value

    def encode(
        self,
        text: str,
        *,
        speaker_map: dict[str, str] | None = None,
        plain_names: bool = False,
    ) -> str:
        if not isinstance(text, str) or not text:
            return text

        if speaker_map:
            def p_ref(match: re.Match[str]) -> str:
                sender_id = speaker_map.get("P" + match.group(1))
                return make_token(sender_id) if sender_id else match.group(0)

            text = P_REF_RE.sub(p_ref, text)

        def label_ref(match: re.Match[str]) -> str:
            sender_id = self.resolve(match.group(1))
            return make_token(sender_id) if sender_id else match.group(1)

        text = LABEL_RE.sub(label_ref, text)
        if plain_names and self._plain_name_to_id:
            for name in sorted(
                self._plain_name_to_id, key=len, reverse=True
            ):
                text = re.sub(
                    re.escape(name),
                    make_token(self._plain_name_to_id[name]),
                    text,
                )
        return text

    def encode_value(
        self,
        value: Any,
        *,
        speaker_map: dict[str, str] | None = None,
        plain_names: bool = False,
    ) -> Any:
        if isinstance(value, str):
            return self.encode(
                value,
                speaker_map=speaker_map,
                plain_names=plain_names,
            )
        if isinstance(value, dict):
            return {
                key: (
                    item
                    if key == "speaker"
                    else self.encode_value(
                        item,
                        speaker_map=speaker_map,
                        plain_names=plain_names,
                    )
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                self.encode_value(
                    item,
                    speaker_map=speaker_map,
                    plain_names=plain_names,
                )
                for item in value
            ]
        return value


def speaker_map_of(speakers: Iterable[Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for speaker in speakers:
        label = str(getattr(speaker, "label", "") or "").strip()
        sender_id = str(getattr(speaker, "sender_id", "") or "").strip()
        if label and sender_id:
            result[label] = sender_id
    return result


def render_profile(
    profile: dict[str, Any] | None,
    catalog: RefCatalog,
    *,
    for_llm: bool,
) -> dict[str, Any]:
    return catalog.render_value(profile or {}, for_llm=for_llm)


def encode_profile(
    profile: dict[str, Any] | None,
    catalog: RefCatalog,
    *,
    speaker_map: dict[str, str] | None = None,
    plain_names: bool = False,
) -> dict[str, Any]:
    return catalog.encode_value(
        profile or {},
        speaker_map=speaker_map,
        plain_names=plain_names,
    )
