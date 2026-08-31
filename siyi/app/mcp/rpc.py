from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.mcp.protocol import JSON_RPC_VERSION
from app.security.trust import detect_prompt_injection, redact_payload, secure_untrusted_payload


def _safe_message(value: object, fallback: str) -> str:
    cleaned, _ = redact_payload(str(value or fallback))
    message = str(cleaned)[:500]
    if detect_prompt_injection(message):
        return "External MCP error message omitted by security policy."
    return message


class McpError(RuntimeError):
    """Base typed MCP failure suitable for tool-result normalization."""

    error_code = "mcp_error"
    retryable = False

    def __init__(self, message: str) -> None:
        self.message = _safe_message(message, "MCP 调用失败")
        super().__init__(self.message)

    def to_tool_failure(self, *, snapshot_id: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "success": False,
            "status": "error",
            "error_code": self.error_code,
            "error_message": _safe_message(self, "MCP 调用失败"),
            "retryable": self.retryable,
        }
        if snapshot_id:
            result["security_snapshot_id"] = snapshot_id
        return result


class McpProtocolError(McpError):
    error_code = "mcp_protocol_error"


class McpTransportError(McpError):
    error_code = "mcp_transport_error"
    retryable = True


class McpRouteRevokedError(McpError):
    error_code = "mcp_route_revoked"


class McpSessionExpiredError(McpTransportError):
    """Only HTTP 404 on an established session permits reinitialization."""

    error_code = "mcp_session_expired"


class McpRpcError(McpError):
    error_code = "mcp_jsonrpc_error"

    def __init__(self, code: int | str, message: str, data: Any = None) -> None:
        self.code = code
        self.message = _safe_message(message, "MCP JSON-RPC error")
        cleaned_data, _ = redact_payload(data)
        self.data = cleaned_data
        # Standard server/internal errors may succeed when retried; invalid requests do not.
        self.retryable = code in {-32603, -32000, -32001, -32002} or (
            isinstance(code, int) and -32099 <= code <= -32000
        )
        super().__init__(self.message)

    def to_tool_failure(self, *, snapshot_id: str | None = None) -> dict[str, Any]:
        result = super().to_tool_failure(snapshot_id=snapshot_id)
        result["rpc_error_code"] = self.code
        if self.data is not None:
            secured, _sensitive, _findings = secure_untrusted_payload(self.data, "mcp:jsonrpc_error")
            result["rpc_error_data"] = secured
        return result


class McpToolResultError(McpError):
    """A valid tools/call response whose domain operation failed."""

    error_code = "mcp_tool_error"

    def __init__(self, result: Mapping[str, Any]) -> None:
        secured, _sensitive, _findings = secure_untrusted_payload(dict(result), "mcp:tool_error")
        self.result = secured if isinstance(secured, dict) else {"isError": True}
        message = "MCP 工具执行失败"
        content = self.result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, Mapping) and item.get("type") == "text" and item.get("text"):
                    message = str(item["text"])
                    break
        super().__init__(message)

    def to_tool_failure(self, *, snapshot_id: str | None = None) -> dict[str, Any]:
        failure = super().to_tool_failure(snapshot_id=snapshot_id)
        failure["mcp_result"] = self.result
        return failure


@dataclass(frozen=True)
class McpRpcResponse:
    request_id: int | str | None
    result: Any = None
    error: McpRpcError | None = None

    @property
    def success(self) -> bool:
        return self.error is None

    @classmethod
    def parse(
        cls,
        payload: Mapping[str, Any] | Any,
        *,
        expected_id: int | str | None = None,
    ) -> "McpRpcResponse":
        if not isinstance(payload, Mapping):
            raise McpProtocolError("MCP 响应不是 JSON 对象")
        version = payload.get("jsonrpc")
        if version is not None and (not isinstance(version, str) or version != JSON_RPC_VERSION):
            raise McpProtocolError("MCP JSON-RPC 版本无效")
        response_id = payload.get("id")
        if expected_id is not None and response_id != expected_id:
            raise McpProtocolError("MCP JSON-RPC 响应 ID 不匹配")
        has_result = "result" in payload
        has_error = "error" in payload
        if has_result == has_error:
            raise McpProtocolError("MCP JSON-RPC 响应必须且只能包含 result 或 error")
        if has_error:
            error = payload.get("error")
            if not isinstance(error, Mapping):
                raise McpProtocolError("MCP JSON-RPC error 必须是对象")
            code = error.get("code", "unknown")
            if not isinstance(code, (int, str)) or isinstance(code, bool):
                code = "unknown"
            return cls(
                request_id=response_id,
                error=McpRpcError(code, str(error.get("message") or "MCP JSON-RPC error"), error.get("data")),
            )
        return cls(request_id=response_id, result=payload.get("result"))

    def raise_for_error(self) -> "McpRpcResponse":
        if self.error is not None:
            raise self.error
        return self

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": JSON_RPC_VERSION, "id": self.request_id}
        if self.error is not None:
            payload["error"] = {
                "code": self.error.code,
                "message": self.error.message,
                **({"data": self.error.data} if self.error.data is not None else {}),
            }
        else:
            payload["result"] = self.result
        return payload


def raise_for_tool_result(method: str, response: McpRpcResponse) -> McpRpcResponse:
    """Map MCP's result-level tool errors into the same failure contract."""

    if method == "tools/call" and isinstance(response.result, Mapping):
        if "isError" in response.result and not isinstance(response.result["isError"], bool):
            raise McpProtocolError("MCP isError 必须是布尔值")
        if response.result.get("isError") is True:
            raise McpToolResultError(response.result)
    return response
