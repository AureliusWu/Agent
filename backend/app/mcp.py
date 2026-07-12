from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import httpx


async def call_http_mcp(url: str, method: str, params: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        response = await client.post(url, headers=headers, json=request)
        response.raise_for_status()
        if "text/event-stream" in response.headers.get("content-type", ""):
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            raise ValueError("MCP SSE 响应中没有 data 事件")
        return response.json()


async def initialize_http_mcp(url: str) -> tuple[str | None, dict[str, Any]]:
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "AureliusWu Agent", "version": "0.2.1"}},
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        response = await client.post(url, headers=headers, json=request)
        response.raise_for_status()
        session_id = response.headers.get("mcp-session-id")
        initialized_headers = dict(headers)
        if session_id:
            initialized_headers["Mcp-Session-Id"] = session_id
        await client.post(url, headers=initialized_headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return session_id, await call_http_mcp(url, "tools/list", {}, session_id)


def call_stdio_mcp(command: str, args: list[str], method: str, params: dict[str, Any]) -> dict[str, Any]:
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
    process = subprocess.run([command, *args], input=request, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45, check=False)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"MCP 进程退出码 {process.returncode}")
    for line in reversed(process.stdout.splitlines()):
        try:
            value = json.loads(line)
            if value.get("id") == 1:
                return value
        except json.JSONDecodeError:
            continue
    raise ValueError("stdio MCP 未返回有效 JSON-RPC 响应")


def mcp_function_name(server_id: int, tool_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", tool_name)[:40]
    return f"mcp__{server_id}__{safe}"


async def discover_mcp_tools(servers: list[dict[str, Any]], allow_local: bool) -> tuple[list[dict[str, Any]], dict[str, tuple[dict[str, Any], str]]]:
    definitions: list[dict[str, Any]] = []
    routes: dict[str, tuple[dict[str, Any], str]] = {}
    for server in servers:
        try:
            if server["transport"] in {"http", "sse"}:
                session_id, response = await initialize_http_mcp(server["url"])
                server = {**server, "_session_id": session_id}
            elif allow_local:
                response = call_stdio_mcp(server["command"], json.loads(server.get("args") or "[]"), "tools/list", {})
            else:
                continue
            for tool in response.get("result", {}).get("tools", []):
                name = mcp_function_name(server["id"], tool["name"])
                definitions.append({"type": "function", "function": {"name": name, "description": f"MCP {server['name']}: {tool.get('description', tool['name'])}", "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}}}})
                routes[name] = (server, tool["name"])
        except Exception:
            continue
    return definitions, routes


async def invoke_mcp_route(route: tuple[dict[str, Any], str], arguments: dict[str, Any], allow_local: bool) -> dict[str, Any]:
    server, tool_name = route
    params = {"name": tool_name, "arguments": arguments}
    if server["transport"] in {"http", "sse"}:
        return await call_http_mcp(server["url"], "tools/call", params, server.get("_session_id"))
    if allow_local:
        return call_stdio_mcp(server["command"], json.loads(server.get("args") or "[]"), "tools/call", params)
    raise ValueError("stdio MCP 仅在桌面本地后端显式启用")
