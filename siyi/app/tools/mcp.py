from __future__ import annotations

"""Compatibility facade for the v15 MCP client architecture.

New code lives under :mod:`app.mcp`. The symbols in this module remain stable
for existing API routes, diagnostics, evals, and third-party extensions.
"""

import asyncio
import json
import subprocess
from typing import Any

from app.mcp.discovery import (
    McpDiscoveryError,
    McpDiscoveryIssue,
    build_tool_definition,
    mcp_function_name,
    normalize_mcp_schema,
    record_discovery_issues,
)
from app.mcp.permissions import (
    redact_bound_value,
    resolve_secret_binding,
    stdio_environment,
    validate_http_server_binding,
    validate_stdio_server_binding,
)
from app.mcp.protocol import request_payload
from app.mcp.rpc import McpProtocolError, McpRouteRevokedError, McpRpcResponse, McpTransportError, raise_for_tool_result
from app.mcp.session import MCPConnectionManager, McpSessionLease, server_set_key
from app.mcp.transports.http import HttpMcpTransport, _response_payload
from app.mcp.transports.stdio import StdioMcpTransport
from app.security.network_security import guarded_request


class _TransportPayload(dict[str, Any]):
    """dict-compatible response that carries an initialized transport privately."""

    def __init__(self, payload: dict[str, Any], transport: Any) -> None:
        super().__init__(payload)
        self.transport = transport


async def call_http_mcp(
    url: str,
    method: str,
    params: dict[str, Any],
    session_id: str | None = None,
    allow_private: bool = False,
    *,
    secret_binding: str | None = None,
) -> dict[str, Any]:
    """Issue an initialized HTTP call and normalize JSON-RPC failures."""

    validate_http_server_binding(url, secret_binding=secret_binding)
    transport = HttpMcpTransport(url, allow_private=allow_private, secret_binding=secret_binding)
    try:
        if session_id is None:
            initialized = await transport.open()
            response = initialized if method == "initialize" else await transport.request(method, params)
        else:
            transport.attach_session(session_id)
            response = await transport.request(method, params)
        return raise_for_tool_result(method, response).as_payload()
    finally:
        # An explicitly supplied session belongs to the legacy caller, not
        # this temporary HTTP client. A replacement session is ours to close.
        await transport.close(terminate_session=session_id is None or transport.session_id != session_id)


async def initialize_http_mcp(
    url: str,
    allow_private: bool = False,
    *,
    secret_binding: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    transport = HttpMcpTransport(url, allow_private=allow_private, secret_binding=secret_binding)
    try:
        await transport.open()
        listed = await transport.request("tools/list", {})
    except BaseException:
        await transport.close()
        raise
    return transport.session_id, _TransportPayload(listed.as_payload(), transport)


def call_stdio_mcp(
    command: str, args: list[str], method: str, params: dict[str, Any], *, secret_binding: str | None = None,
) -> dict[str, Any]:
    """Legacy synchronous one-shot client retained for external compatibility."""

    validate_stdio_server_binding(command, args, secret_binding=secret_binding)
    environment = stdio_environment(secret_binding)
    bound = resolve_secret_binding(secret_binding)
    request = json.dumps(request_payload(1, method, params), ensure_ascii=False) + "\n"
    try:
        process = subprocess.run(
            [command, *args],
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise McpTransportError("stdio MCP 调用失败或超时") from exc
    if process.returncode != 0:
        raise McpTransportError(f"MCP 进程退出码 {process.returncode}")
    for line in reversed(process.stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("id") == 1:
            parsed = McpRpcResponse.parse(redact_bound_value(value, bound[1] if bound else None), expected_id=1)
            parsed.raise_for_error()
            return raise_for_tool_result(method, parsed).as_payload()
    raise McpProtocolError("stdio MCP 未返回有效 JSON-RPC 响应")


async def call_stdio_mcp_async(
    command: str,
    args: list[str],
    method: str,
    params: dict[str, Any],
    *,
    secret_binding: str | None = None,
) -> dict[str, Any]:
    """Initialized one-call stdio client; discovery routes use persistent sessions."""

    transport = StdioMcpTransport(command, args, secret_binding=secret_binding)
    try:
        initialized = await transport.open()
        response = initialized if method == "initialize" else await transport.request(method, params)
        return response.as_payload()
    finally:
        # Cancellation must still reclaim the supervised child process.
        await asyncio.shield(transport.close())


# Backwards-compatible private aliases used by the v14 runtime test seam.
_normalize_mcp_schema = normalize_mcp_schema
_server_set_key = server_set_key


MCP_CONNECTIONS = MCPConnectionManager()


async def _discover_mcp_tools_uncached(
    servers: list[dict[str, Any]],
    allow_local: bool,
    *,
    cache_key: str,
) -> tuple[list[dict[str, Any]], dict[str, tuple[dict[str, Any], str]]]:
    definitions: list[dict[str, Any]] = []
    routes: dict[str, tuple[dict[str, Any], str]] = {}
    issues: list[McpDiscoveryIssue] = []
    completed_servers = 0
    for configured_server in servers:
        server = dict(configured_server)
        lease = server.get("_mcp_lease")
        resource_id = str(server.get("id") or server_set_key([server], allow_local))
        binding = server.get("secret_binding")
        transport: Any | None = None
        try:
            if isinstance(lease, McpSessionLease):
                transport = lease.resource(resource_id)
            if transport is not None:
                if not transport.initialized:
                    await transport.open()
                response = (await transport.request("tools/list", {})).as_payload()
                server["_session_id"] = getattr(transport, "session_id", None)
            elif server.get("transport") in {"http", "sse"}:
                binding_kwargs = {"secret_binding": binding} if binding else {}
                session_id, response = await initialize_http_mcp(str(server["url"]), allow_local, **binding_kwargs)
                transport = getattr(response, "transport", None)
                server["_session_id"] = session_id
            elif server.get("transport") == "stdio":
                if not allow_local:
                    raise PermissionError("stdio MCP 仅在桌面本地后端显式启用")
                raw_args = server.get("args") or "[]"
                args = json.loads(raw_args) if isinstance(raw_args, str) else list(raw_args)
                if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                    raise McpProtocolError("stdio MCP args 必须是字符串数组")
                transport = StdioMcpTransport(str(server["command"]), args, secret_binding=binding)
                if isinstance(lease, McpSessionLease):
                    lease.track(resource_id, transport)
                await transport.open()
                response = (await transport.request("tools/list", {})).as_payload()
            else:
                raise McpProtocolError(f"不支持的 MCP transport: {server.get('transport')}")

            if isinstance(lease, McpSessionLease):
                if transport is not None:
                    lease.track(resource_id, transport)
                lease.check()
            server["_session_cache_key"] = cache_key
            rpc = McpRpcResponse.parse(response)
            rpc.raise_for_error()
            result = rpc.result
            tools = result.get("tools") if isinstance(result, dict) else None
            if not isinstance(tools, list):
                raise McpProtocolError("MCP tools/list 未返回 tools 数组")
            if transport is not None:
                server["_mcp_transport"] = transport
            completed_servers += 1
            for tool in tools:
                built = build_tool_definition(server, tool)
                if built is None:
                    continue
                definition, tool_name = built
                name = definition["function"]["name"]
                definitions.append(definition)
                routes[name] = (server, tool_name)
        except BaseException as exc:
            if transport is not None:
                try:
                    await transport.close()
                except (OSError, RuntimeError):
                    pass
            if isinstance(exc, (asyncio.CancelledError, McpRouteRevokedError)):
                raise
            if not isinstance(exc, Exception):
                raise
            issues.append(McpDiscoveryIssue.from_exception(server, exc))

    record_discovery_issues(cache_key, issues)
    if issues and completed_servers == 0:
        raise McpDiscoveryError(issues)
    return definitions, routes


async def discover_mcp_tools(
    servers: list[dict[str, Any]],
    allow_local: bool,
) -> tuple[list[dict[str, Any]], dict[str, tuple[dict[str, Any], str]]]:
    return await MCP_CONNECTIONS.discover(servers, allow_local)


async def invoke_mcp_route(
    route: tuple[dict[str, Any], str],
    arguments: dict[str, Any],
    allow_local: bool,
) -> dict[str, Any]:
    server, tool_name = route
    lease = server.get("_mcp_lease")
    if lease is not None:
        if not isinstance(lease, McpSessionLease):
            raise McpRouteRevokedError("MCP 路由授权无效")
        lease.check()
    if server.get("enabled") == 0:
        raise McpRouteRevokedError("MCP 服务已禁用")
    if server.get("transport") == "stdio" and not allow_local:
        raise McpRouteRevokedError("stdio MCP 仅在桌面本地后端显式启用")
    method = str(server.get("_mcp_rpc_method") or "tools/call")
    if method not in {"tools/call", "tools/list", "ping"}:
        raise McpProtocolError("不支持直接调用该 MCP 方法")
    params = arguments if server.get("_mcp_params_passthrough") else {"name": tool_name, "arguments": arguments}
    transport = server.get("_mcp_transport")
    if transport is None and isinstance(lease, McpSessionLease):
        resource_id = str(server.get("id") or server_set_key([server], allow_local))
        transport = lease.resource(resource_id)
        if transport is None:
            if server.get("transport") in {"http", "sse"}:
                transport = HttpMcpTransport(str(server["url"]), allow_private=allow_local, secret_binding=server.get("secret_binding"))
            elif server.get("transport") == "stdio":
                raw_args = server.get("args") or []
                args = json.loads(raw_args) if isinstance(raw_args, str) else list(raw_args)
                transport = StdioMcpTransport(str(server["command"]), args, secret_binding=server.get("secret_binding"))
            else:
                raise McpProtocolError("不支持的 MCP transport")
            lease.track(resource_id, transport)
        server["_mcp_transport"] = transport
    if transport is not None:
        if server.get("transport") in {"http", "sse"}:
            transport.allow_private = allow_local
        return raise_for_tool_result(method, await transport.request(method, params)).as_payload()

    if server.get("transport") in {"http", "sse"}:
        return await call_http_mcp(
            str(server["url"]), method, params, server.get("_session_id"), allow_local,
            secret_binding=server.get("secret_binding"),
        )
    if server.get("transport") == "stdio" and allow_local:
        raw_args = server.get("args") or "[]"
        args = json.loads(raw_args) if isinstance(raw_args, str) else list(raw_args)
        return await call_stdio_mcp_async(str(server["command"]), args, method, params, secret_binding=server.get("secret_binding"))
    raise PermissionError("stdio MCP 仅在桌面本地后端显式启用")


__all__ = [
    "MCP_CONNECTIONS",
    "MCPConnectionManager",
    "call_http_mcp",
    "call_stdio_mcp",
    "call_stdio_mcp_async",
    "discover_mcp_tools",
    "initialize_http_mcp",
    "invoke_mcp_route",
    "mcp_function_name",
]
