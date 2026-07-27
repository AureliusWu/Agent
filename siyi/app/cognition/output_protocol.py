from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_PROTOCOL_TAG = re.compile(
    r"<\s*/?\s*(?:(?:｜｜|\|\|)DSML(?:｜｜|\|\|)\s*)?"
    r"(?:web_search|tool_call|tool_calls|function_call|function_calls|invoke|parameter|search_quality_reminder)\b",
    re.IGNORECASE,
)

_DSML_INVOKE = re.compile(
    r"<\s*(?:｜｜|\|\|)DSML(?:｜｜|\|\|)invoke\s+name=[\"'](?P<name>[A-Za-z0-9_.:-]+)[\"']\s*>"
    r"(?P<body>.*?)"
    r"<\s*/\s*(?:｜｜|\|\|)DSML(?:｜｜|\|\|)invoke\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_PARAMETER = re.compile(
    r"<\s*(?:｜｜|\|\|)DSML(?:｜｜|\|\|)parameter\s+"
    r"name=[\"'](?P<name>[A-Za-z0-9_.:-]+)[\"']"
    r"(?:\s+string=[\"'](?P<string>true|false)[\"'])?\s*>"
    r"(?P<value>.*?)"
    r"<\s*/\s*(?:｜｜|\|\|)DSML(?:｜｜|\|\|)parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_TOOL_CALLS_START = re.compile(
    r"<\s*(?:｜｜|\|\|)DSML(?:｜｜|\|\|)tool_calls\s*>", re.IGNORECASE
)

UNEXECUTED_TOOL_NOTICE = "模型请求了未注册或未执行的工具，未获得任何工具结果。"


def parse_deepseek_text_tool_calls(
    content: str,
    tools: list[dict[str, Any]] | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Convert DeepSeek DSML fallback output into validated OpenAI tool calls."""
    if not content or not tools or "DSML" not in content:
        return content, []
    schemas: dict[str, tuple[dict[str, Any], set[str]]] = {}
    for tool in tools:
        function = tool.get("function") or {}
        parameters = function.get("parameters") or {}
        schemas[str(function.get("name") or "")] = (
            dict(parameters.get("properties") or {}),
            set(parameters.get("required") or []),
        )
    calls: list[dict[str, Any]] = []
    for index, match in enumerate(_DSML_INVOKE.finditer(content)):
        name = match.group("name")
        schema = schemas.get(name)
        if schema is None:
            continue
        properties, required = schema
        arguments: dict[str, Any] = {}
        for parameter in _DSML_PARAMETER.finditer(match.group("body")):
            parameter_name = parameter.group("name")
            if parameter_name not in properties:
                continue
            raw = parameter.group("value").strip()
            if parameter.group("string") != "false":
                value: Any = raw
            else:
                try:
                    value = json.loads(raw)
                except ValueError:
                    value = raw
            arguments[parameter_name] = value
        if not required.issubset(arguments):
            continue
        digest = hashlib.sha256(f"{name}:{index}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}".encode()).hexdigest()[:20]
        calls.append(
            {
                "id": f"call_dsml_{digest}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                },
            }
        )
    if not calls:
        return content, []
    starts = [match.start() for match in _DSML_INVOKE.finditer(content)]
    wrapper = _DSML_TOOL_CALLS_START.search(content)
    if wrapper:
        starts.append(wrapper.start())
    first = min(starts)
    visible = content[:first].strip()
    return visible, calls


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
                break
            emitted.append(candidate)
        return "".join(emitted)

    def finish(self) -> str:
        if self.blocked:
            return ""
        value, self._pending = self._pending, ""
        return value
