from __future__ import annotations

from dataclasses import asdict, dataclass
import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import re
import time
from typing import Any

from app.mcp.rpc import McpProtocolError, McpRpcResponse
from app.security.trust import detect_prompt_injection, redact_payload


LOGGER = logging.getLogger(__name__)
_DISCOVERY_REPORTS: dict[str, list[dict[str, Any]]] = {}
MAX_DISCOVERY_PAGES = 32
MAX_DISCOVERY_TOOLS = 512
MAX_DISCOVERY_BYTES = 4 * 1024 * 1024
MAX_DISCOVERY_CURSOR_BYTES = 2048
DISCOVERY_TIMEOUT_SECONDS = 45.0


class McpDiscoveryLimitError(McpProtocolError):
    error_code = "mcp_discovery_limit"


class McpDiscoveryBudget:
    """One bounded tools/list operation, shared across every server and page."""

    def __init__(self) -> None:
        self.deadline = time.monotonic() + DISCOVERY_TIMEOUT_SECONDS
        self.pages = 0
        self.tools = 0
        self.bytes = 0

    def check_request(self) -> None:
        if time.monotonic() >= self.deadline:
            raise McpDiscoveryLimitError("MCP tools/list 发现总时限已用尽")
        if self.pages >= MAX_DISCOVERY_PAGES:
            raise McpDiscoveryLimitError("MCP tools/list 总页数超过安全上限")

    def consume(self, payload: dict[str, Any]) -> None:
        self.check_request()
        self.pages += 1
        # iterencode avoids allocating an additional complete serialized copy.
        try:
            for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(payload):
                self.bytes += len(chunk.encode("utf-8"))
                if self.bytes > MAX_DISCOVERY_BYTES:
                    raise McpDiscoveryLimitError("MCP tools/list 总字节数超过安全上限")
        except (TypeError, ValueError, RecursionError) as exc:
            raise McpProtocolError("MCP tools/list 包含无效 JSON 数据") from exc


def _validate_input_schema(schema: Any, depth: int = 0) -> None:
    """Reject malformed/unrepresentable shapes before the legacy sanitizer.

    Descriptions and external references still pass through the existing
    prompt-injection/redaction boundary; this does not fetch or resolve $ref.
    """
    if not isinstance(schema, dict) or depth > 5:
        raise McpProtocolError("MCP inputSchema 必须是深度不超过 5 的 schema 对象")
    allowed_types = {"object", "array", "string", "integer", "number", "boolean", "null"}
    schema_type = schema.get("type")
    if schema_type is not None and (not isinstance(schema_type, str) or schema_type not in allowed_types):
        raise McpProtocolError("MCP inputSchema type 无效或不受支持")
    if depth == 0 and schema_type not in {None, "object"}:
        raise McpProtocolError("MCP 工具 inputSchema 必须为 object")
    if "description" in schema and not isinstance(schema["description"], str):
        raise McpProtocolError("MCP inputSchema description 必须为字符串")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or len(properties) > 64:
        raise McpProtocolError("MCP inputSchema properties 无效或超过安全上限")
    for key, child in properties.items():
        if not isinstance(key, str) or not key or len(key) > 100:
            raise McpProtocolError("MCP inputSchema 字段名无效")
        _validate_input_schema(child, depth + 1)
    if "required" in schema:
        required = schema["required"]
        if (not isinstance(required, list) or len(required) > 64
                or any(not isinstance(key, str) or key not in properties for key in required)
                or len(set(required)) != len(required)):
            raise McpProtocolError("MCP inputSchema required 无效")
    if "items" in schema:
        _validate_input_schema(schema["items"], depth + 1)
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], (bool, dict)):
        raise McpProtocolError("MCP inputSchema additionalProperties 无效")
    if "enum" in schema:
        values = schema["enum"]
        if (not isinstance(values, list) or not values or len(values) > 50
                or any(not isinstance(value, (str, int, float, bool)) and value is not None for value in values)):
            raise McpProtocolError("MCP inputSchema enum 无效或超过安全上限")


async def collect_tool_pages(
    initial_response: dict[str, Any],
    request_page: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    check_authorized: Callable[[], None],
    budget: McpDiscoveryBudget,
) -> list[dict[str, Any]]:
    """Stage a whole server listing; a partial result is never returned."""
    tools: list[dict[str, Any]] = []
    names: set[str] = set()
    aliases: set[str] = set()
    cursors: set[str] = set()
    response = initial_response
    while True:
        check_authorized()
        budget.consume(response)
        rpc = McpRpcResponse.parse(response).raise_for_error()
        result = rpc.result
        page_tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(page_tools, list):
            raise McpProtocolError("MCP tools/list 未返回 tools 数组")
        budget.tools += len(page_tools)
        if budget.tools > MAX_DISCOVERY_TOOLS:
            raise McpDiscoveryLimitError("MCP tools/list 工具总量超过安全上限")
        for tool in page_tools:
            name = tool.get("name") if isinstance(tool, dict) else None
            if (not isinstance(name, str) or not name.strip() or name != name.strip()
                    or len(name) > 100 or any(ord(character) < 32 for character in name)):
                raise McpProtocolError("MCP tools/list 工具名称无效")
            alias = mcp_function_name(0, name)
            if name in names or alias in aliases:
                raise McpProtocolError("MCP tools/list 工具名称重复或规范化路由冲突")
            # Older integrations omit inputSchema for no-argument tools. Keep
            # that compatibility, but an explicitly malformed schema must fail.
            _validate_input_schema(tool.get("inputSchema", {}))
            names.add(name)
            aliases.add(alias)
            tools.append(tool)
        if "nextCursor" not in result:
            check_authorized()
            return tools
        cursor = result["nextCursor"]
        if not isinstance(cursor, str) or not cursor or len(cursor.encode("utf-8")) > MAX_DISCOVERY_CURSOR_BYTES:
            raise McpProtocolError("MCP tools/list nextCursor 无效或超过安全上限")
        if cursor in cursors:
            raise McpProtocolError("MCP tools/list 返回重复 cursor")
        cursors.add(cursor)
        check_authorized()
        budget.check_request()
        try:
            async with asyncio.timeout_at(budget.deadline):
                response = await request_page({"cursor": cursor})
        except TimeoutError as exc:
            raise McpDiscoveryLimitError("MCP tools/list 发现总时限已用尽") from exc


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
