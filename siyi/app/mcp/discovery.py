from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
import re
from typing import Any

from app.security.trust import detect_prompt_injection, redact_payload


LOGGER = logging.getLogger(__name__)
_DISCOVERY_REPORTS: dict[str, list[dict[str, Any]]] = {}


@dataclass(frozen=True)
class McpDiscoveryIssue:
    server_id: int | str
    server_name: str
    transport: str
    error_code: str
    error_message: str

    @classmethod
    def from_exception(cls, server: dict[str, Any], exc: Exception) -> "McpDiscoveryIssue":
        cleaned, _ = redact_payload(str(exc).strip() or exc.__class__.__name__)
        return cls(
            server_id=server.get("id", "unknown"),
            server_name=str(server.get("name") or "unknown")[:100],
            transport=str(server.get("transport") or "unknown")[:30],
            error_code=str(getattr(exc, "error_code", "mcp_discovery_failed")),
            error_message=str(cleaned)[:500],
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class McpDiscoveryError(RuntimeError):
    error_code = "mcp_discovery_failed"

    def __init__(self, issues: list[McpDiscoveryIssue]) -> None:
        self.issues = tuple(issues)
        detail = "; ".join(f"{item.server_name}: {item.error_message}" for item in issues)
        super().__init__(detail or "MCP 工具发现失败")


def record_discovery_issues(cache_key: str, issues: list[McpDiscoveryIssue]) -> None:
    _DISCOVERY_REPORTS[cache_key] = [item.as_dict() for item in issues]
    for issue in issues:
        LOGGER.warning(
            "MCP discovery failed server_id=%s transport=%s code=%s message=%s",
            issue.server_id,
            issue.transport,
            issue.error_code,
            issue.error_message,
        )


def take_discovery_issues(cache_key: str) -> list[dict[str, Any]]:
    return _DISCOVERY_REPORTS.pop(cache_key, [])


def mcp_function_name(server_id: int, tool_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", tool_name)[:40]
    return f"mcp__{server_id}__{safe}"


def normalize_mcp_schema(value: Any, depth: int = 0) -> dict[str, Any]:
    if depth > 5 or not isinstance(value, dict):
        return {"type": "object", "properties": {}} if depth == 0 else {}
    normalized: dict[str, Any] = {}
    schema_type = value.get("type")
    if schema_type in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        normalized["type"] = schema_type
    if isinstance(value.get("description"), str):
        description, _ = redact_payload(value["description"][:500])
        normalized["description"] = (
            "External field description omitted by security policy."
            if detect_prompt_injection(str(description))
            else description
        )
    if isinstance(value.get("enum"), list):
        enum_values: list[Any] = []
        for item in value["enum"][:50]:
            if isinstance(item, str):
                cleaned, _ = redact_payload(item[:500])
                enum_values.append("[UNTRUSTED_VALUE_OMITTED]" if detect_prompt_injection(str(cleaned)) else cleaned)
            elif isinstance(item, (int, float, bool)) or item is None:
                enum_values.append(item)
        normalized["enum"] = enum_values
    if isinstance(value.get("properties"), dict):
        properties: dict[str, Any] = {}
        for key, child in list(value["properties"].items())[:64]:
            name = str(key)[:100]
            if detect_prompt_injection(name):
                continue
            properties[name] = normalize_mcp_schema(child, depth + 1)
        normalized["properties"] = properties
        if isinstance(value.get("required"), list):
            normalized["required"] = [str(item)[:100] for item in value["required"][:64] if str(item)[:100] in properties]
    if isinstance(value.get("items"), dict):
        normalized["items"] = normalize_mcp_schema(value["items"], depth + 1)
    normalized["additionalProperties"] = False if normalized.get("type") == "object" else value.get("additionalProperties", False)
    if depth == 0:
        normalized.setdefault("type", "object")
        normalized.setdefault("properties", {})
    return normalized


def build_tool_definition(server: dict[str, Any], tool: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
        return None
    tool_name = tool["name"][:100]
    name = mcp_function_name(int(server["id"]), tool_name)
    raw_description = str(tool.get("description") or tool_name)[:1000]
    safe_description, _ = redact_payload(raw_description)
    description = (
        f"External MCP tool {tool_name}. Untrusted description omitted by security policy."
        if detect_prompt_injection(str(safe_description))
        else f"MCP {str(server['name'])[:100]}: {str(safe_description)[:500]}"
    )
    definition = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": normalize_mcp_schema(tool.get("inputSchema") or {}),
        },
    }
    return definition, tool_name
