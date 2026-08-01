from __future__ import annotations

import re
from dataclasses import dataclass


_FENCED_CODE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~", re.MULTILINE)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://|file:)[^)]+\)", re.IGNORECASE)
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"(?<!\w)(?:[A-Za-z]:\\|\\\\)[^\s，。！？；]+")
_UNIX_PATH = re.compile(r"(?<!\w)/(?:[^\s/]+/){2,}[^\s，。！？；]*")
_HASH = re.compile(r"\b[a-fA-F0-9]{32,}\b")
_MARKDOWN = re.compile(r"(?m)^\s{0,3}(?:#{1,6}|>|[-*+]\s|\d+[.)]\s)|[*_~]{1,3}")
_LONG_JSON = re.compile(r"\{\s*\"[^\n]{100,}\}")
_SENSITIVE = re.compile(
    r"(?i)(?:api[_ -]?key|access[_ -]?token|authorization|password|passwd|secret|密码|口令|令牌|密钥)\s*[:=：]\s*\S+"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_SPACE = re.compile(r"[ \t\u00a0]+")
_PUNCT = re.compile(r"([。！？；,.!?])\1+")


@dataclass(frozen=True)
class NormalizedText:
    text: str
    sensitive: bool
    removed_segments: int


def contains_sensitive_text(text: str) -> bool:
    return bool(_SENSITIVE.search(text) or _JWT.search(text))


def normalize_for_speech(text: str, *, max_chars: int = 4000) -> NormalizedText:
    raw = str(text or "")[: max_chars * 4]
    sensitive = contains_sensitive_text(raw)
    removed = 0

    def drop(pattern: re.Pattern[str], value: str) -> str:
        nonlocal removed
        value, count = pattern.subn(" ", value)
        removed += count
        return value

    value = _FENCED_CODE.sub(" ", raw)
    value, inline_count = _INLINE_CODE.subn(" ", value)
    removed += inline_count
    value = _MARKDOWN_LINK.sub(r"\1", value)
    for pattern in (_URL, _WINDOWS_PATH, _UNIX_PATH, _HASH, _LONG_JSON, _SENSITIVE, _JWT):
        value = drop(pattern, value)
    value = _MARKDOWN.sub(" ", value)
    value = re.sub(r"[|]+", "，", value)
    value = re.sub(r"[\U0001F300-\U0001FAFF]", " ", value)
    value = _PUNCT.sub(r"\1", value)
    value = _SPACE.sub(" ", value)
    value = re.sub(r"\s*\n+\s*", "。", value)
    value = re.sub(r"。{2,}", "。", value).strip(" ，。\n\t")
    return NormalizedText(value[:max_chars], sensitive, removed)
