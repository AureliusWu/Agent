from __future__ import annotations

import re


_PROTOCOL_TAG = re.compile(
    r"<\s*/?\s*(?:web_search|tool_call|tool_calls|function_call|function_calls|invoke|search_quality_reminder)\b",
    re.IGNORECASE,
)

UNEXECUTED_TOOL_NOTICE = "模型请求了未注册或未执行的工具，未获得任何工具结果。"


def contains_unexecuted_tool_protocol(content: str, *, has_native_tool_calls: bool = False) -> bool:
    """Detect provider text that impersonates a tool protocol without a real ToolCall."""
    return bool(content and not has_native_tool_calls and _PROTOCOL_TAG.search(content))


def sanitize_unexecuted_tool_protocol(content: str, *, has_native_tool_calls: bool = False) -> str:
    if contains_unexecuted_tool_protocol(content, has_native_tool_calls=has_native_tool_calls):
        return UNEXECUTED_TOOL_NOTICE
    return content


class StreamingProtocolGuard:
    """Pass normal text through while withholding protocol-looking XML fragments."""

    def __init__(self) -> None:
        self._pending = ""
        self.blocked = False
        self._notice_emitted = False

    def feed(self, delta: str) -> str:
        if not delta or self.blocked:
            return ""
        self._pending += delta
        emitted: list[str] = []
        while self._pending:
            marker = self._pending.find("<")
            if marker < 0:
                emitted.append(self._pending)
                self._pending = ""
                break
            if marker > 0:
                emitted.append(self._pending[:marker])
                self._pending = self._pending[marker:]
            end = self._pending.find(">")
            if end < 0:
                break
            candidate = self._pending[: end + 1]
            self._pending = self._pending[end + 1 :]
            if _PROTOCOL_TAG.search(candidate):
                self.blocked = True
                self._pending = ""
                if not self._notice_emitted:
                    emitted.append(f"\n\n{UNEXECUTED_TOOL_NOTICE}")
                    self._notice_emitted = True
                break
            emitted.append(candidate)
        return "".join(emitted)

    def finish(self) -> str:
        if self.blocked:
            return ""
        value, self._pending = self._pending, ""
        return value
