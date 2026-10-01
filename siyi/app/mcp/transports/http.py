from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx

from app.mcp.discovery import McpDiscoveryBudget, collect_tool_pages
from app.mcp.permissions import McpSecretBindingError, normalize_secret_binding, redact_bound_value, resolve_secret_binding, validate_http_server_binding
from app.mcp.protocol import initialize_params, notification_payload, request_payload
from app.mcp.rpc import McpProtocolError, McpRpcResponse, McpSessionExpiredError, McpTransportError, raise_for_tool_result
from app.security.network_security import NetworkPolicyError, guarded_request


def _messages(value: Any) -> list[dict[str, Any]]:
    candidates = value if isinstance(value, list) else [value]
    messages = [dict(item) for item in candidates if isinstance(item, dict)]
    if not messages or len(messages) != len(candidates):
        raise McpProtocolError("MCP HTTP 响应包含无效 JSON-RPC 消息")
    return messages


def _response_payload(
    response: httpx.Response,
    *,
    expected_id: int | str | None = None,
) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").casefold()
    decoded: list[dict[str, Any]] = []
    try:
        if "text/event-stream" not in content_type:
            decoded.extend(_messages(response.json()))
        else:
            data_lines: list[str] = []

            def flush_event() -> None:
                if not data_lines:
                    return
                decoded.extend(_messages(json.loads("\n".join(data_lines))))
                data_lines.clear()

            for raw_line in response.text.splitlines():
                line = raw_line.rstrip("\r")
                if not line:
                    flush_event()
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            flush_event()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise McpProtocolError("MCP HTTP 响应 JSON 无法解析") from exc

    if not decoded:
        raise McpProtocolError("MCP HTTP 响应中没有 JSON-RPC 消息")
    if expected_id is not None:
        for message in decoded:
            if message.get("id") == expected_id and ("result" in message or "error" in message):
                return message
        raise McpProtocolError("MCP HTTP 响应中没有匹配的 JSON-RPC response")
    for message in decoded:
        if "result" in message or "error" in message:
            return message
    raise McpProtocolError("MCP HTTP 响应中没有 JSON-RPC response")


class HttpMcpTransport:
    def __init__(
        self,
        url: str,
        *,
        allow_private: bool = False,
        timeout: float = 45.0,
        secret_binding: str | None = None,
    ) -> None:
        validate_http_server_binding(url, secret_binding=secret_binding)
        self.url = url
        self.allow_private = allow_private
        self.timeout = timeout
        self.secret_binding = normalize_secret_binding(secret_binding)
        self.session_id: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._next_id = 1
        self._initialized = False
        self._lock = asyncio.Lock()
        self.authorization_check: Callable[[], None] | None = None
        self._bound_secret_value: str | None = None

    @property
    def initialized(self) -> bool:
        return self._initialized

    def attach_session(self, session_id: str) -> None:
        """Compatibility entry for an already initialized caller-owned session."""
        if self._client is not None or self._initialized:
            raise McpProtocolError("不能替换已使用的 MCP session")
        self.session_id = session_id or None
        self._initialized = True

    def _check_authorized(self) -> None:
        if self.authorization_check is not None:
            self.authorization_check()

    def _headers(self) -> dict[str, str]:
        binding = resolve_secret_binding(self.secret_binding)
        self._bound_secret_value = binding[1] if binding is not None else None
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **({"Authorization": f"Bearer {binding[1]}"} if binding is not None else {}),
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def _post(self, payload: dict[str, Any], *, purpose: str) -> httpx.Response:
        self._check_authorized()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
        try:
            response = await guarded_request(
                self._client,
                "POST",
                self.url,
                purpose=purpose,
                headers=self._headers(),
                json=payload,
                allow_private=self.allow_private,
            )
        except McpSecretBindingError as exc:
            raise McpTransportError("MCP 密钥引用不可用") from exc
        except (httpx.RequestError, NetworkPolicyError, OSError) as exc:
            raise McpTransportError("MCP HTTP 传输失败") from exc
        if response.status_code == 404 and self.session_id and purpose == "remote_mcp":
            raise McpSessionExpiredError("MCP HTTP session 已失效")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Do not expose credential-bearing URLs or HTTP response bodies.
            raise McpTransportError(f"MCP HTTP 状态失败 ({response.status_code})") from exc
        return response

    async def _open_unlocked(self) -> McpRpcResponse:
        self._check_authorized()
        if self._initialized:
            raise McpProtocolError("MCP HTTP session 已初始化")
        request_id = self._next_id
        self._next_id += 1
        try:
            response = await self._post(
                request_payload(request_id, "initialize", initialize_params()),
                purpose="remote_mcp_initialize",
            )
            self.session_id = response.headers.get("mcp-session-id")
            initialized = McpRpcResponse.parse(
                redact_bound_value(_response_payload(response, expected_id=request_id), self._bound_secret_value),
                expected_id=request_id,
            )
            initialized.raise_for_error()
            await self._post(
                notification_payload("notifications/initialized"),
                purpose="remote_mcp_initialize",
            )
            self._initialized = True
            return initialized
        except BaseException:
            await self._close_unlocked()
            raise

    async def open(self) -> McpRpcResponse:
        try:
            async with asyncio.timeout(self.timeout):
                async with self._lock:
                    return await self._open_unlocked()
        except TimeoutError as exc:
            await self.close(terminate_session=False)
            raise McpTransportError("MCP HTTP 初始化超时") from exc

    async def _request_unlocked(self, method: str, params: dict[str, Any], *, retry_session: bool = True) -> McpRpcResponse:
        self._check_authorized()
        if not self._initialized:
            await self._open_unlocked()
        request_id = self._next_id
        self._next_id += 1
        try:
            response = await self._post(
                request_payload(request_id, method, params),
                purpose="remote_mcp",
            )
        except McpSessionExpiredError:
            if not retry_session:
                raise
            # Reinitialize the same owned transport only for the protocol's
            # explicit session-expired 404, never on 409/410/500 or a timeout.
            await self._close_unlocked(terminate_session=False)
            await self._open_unlocked()
            if method == "tools/call":
                listed = await self._request_unlocked("tools/list", {}, retry_session=False)

                async def request_page(page_params: dict[str, Any]) -> dict[str, Any]:
                    # Reconnect validation runs under the existing request
                    # lock/deadline; another expired session may not recurse.
                    response = await self._request_unlocked("tools/list", page_params, retry_session=False)
                    return response.as_payload()

                tools = await collect_tool_pages(
                    listed.as_payload(), request_page,
                    check_authorized=self._check_authorized, budget=McpDiscoveryBudget(),
                )
                if not any(tool.get("name") == params.get("name") for tool in tools):
                    raise McpProtocolError("MCP 重连后工具不再可用")
            return await self._request_unlocked(method, params, retry_session=False)
        parsed = McpRpcResponse.parse(
            redact_bound_value(_response_payload(response, expected_id=request_id), self._bound_secret_value),
            expected_id=request_id,
        )
        parsed.raise_for_error()
        return raise_for_tool_result(method, parsed)

    async def request(self, method: str, params: dict[str, Any]) -> McpRpcResponse:
        try:
            async with asyncio.timeout(self.timeout):
                async with self._lock:
                    return await self._request_unlocked(method, params)
        except TimeoutError as exc:
            await self.close(terminate_session=False)
            raise McpTransportError("MCP HTTP 调用总时限已用尽") from exc

    async def _close_unlocked(self, *, terminate_session: bool = True) -> None:
        client, self._client = self._client, None
        if client is None:
            self._initialized = False
            self.session_id = None
            return
        try:
            if terminate_session and self.session_id:
                try:
                    async with asyncio.timeout(min(self.timeout, 3.0)):
                        response = await guarded_request(
                            client,
                            "DELETE",
                            self.url,
                            purpose="remote_mcp_close",
                            headers=self._headers(),
                            allow_private=self.allow_private,
                        )
                    if response.status_code not in {200, 202, 204, 404, 405}:
                        response.raise_for_status()
                except (TimeoutError, httpx.HTTPError, NetworkPolicyError, McpSecretBindingError, OSError):
                    # The local client must still be closed; remote session expiry
                    # is safe and is surfaced by the next initialization attempt.
                    pass
        finally:
            await client.aclose()
            self._initialized = False
            self.session_id = None
            self._bound_secret_value = None

    async def close(self, *, terminate_session: bool = True) -> None:
        async with self._lock:
            await self._close_unlocked(terminate_session=terminate_session)
