from __future__ import annotations

import asyncio
import hashlib
import os
import time

import json
import re
import subprocess
from typing import Any

import httpx

from . import __version__
from .config import settings
from .network_security import guarded_request
from .trust import detect_prompt_injection, redact_payload


async def call_http_mcp(url: str, method: str, params: dict[str, Any], session_id: str | None = None, allow_private: bool = False) -> dict[str, Any]:
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
        response = await guarded_request(client, "POST", url, purpose="remote_mcp", headers=headers, json=request, allow_private=allow_private)
        response.raise_for_status()
        if "text/event-stream" in response.headers.get("content-type", ""):
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            raise ValueError("MCP SSE 响应中没有 data 事件")
        return response.json()


async def initialize_http_mcp(url: str, allow_private: bool = False) -> tuple[str | None, dict[str, Any]]:
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "AureliusWu Agent", "version": __version__}},
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
        response = await guarded_request(client, "POST", url, purpose="remote_mcp_initialize", headers=headers, json=request, allow_private=allow_private)
        response.raise_for_status()
        session_id = response.headers.get("mcp-session-id")
        initialized_headers = dict(headers)
        if session_id:
            initialized_headers["Mcp-Session-Id"] = session_id
        await guarded_request(client, "POST", url, purpose="remote_mcp_initialize", headers=initialized_headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, allow_private=allow_private)
    return session_id, await call_http_mcp(url, "tools/list", {}, session_id, allow_private)


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


async def call_stdio_mcp_async(command: str, args: list[str], method: str, params: dict[str, Any]) -> dict[str, Any]:
    request = (json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n").encode()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = await asyncio.create_subprocess_exec(command, *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=creationflags)
    from .process_supervisor import register_process, terminate_process_tree, unregister_process

    register_process(process.pid, None, command, args, process)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(request), timeout=45)
    except asyncio.CancelledError:
        terminate_process_tree(process.pid)
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            current.uncancel()
        await process.wait()
        unregister_process(process.pid, "cancelled")
        raise
    except TimeoutError:
        terminate_process_tree(process.pid)
        await process.wait()
        unregister_process(process.pid, "timed_out")
        raise TimeoutError("stdio MCP 调用超时")
    unregister_process(process.pid)
    if process.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or f"MCP 进程退出码 {process.returncode}")
    for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
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


def _normalize_mcp_schema(value: Any, depth: int = 0) -> dict[str, Any]:
    if depth > 5 or not isinstance(value, dict):
        return {"type": "object", "properties": {}} if depth == 0 else {}
    normalized: dict[str, Any] = {}
    schema_type = value.get("type")
    if schema_type in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        normalized["type"] = schema_type
    if isinstance(value.get("description"), str):
        description, _ = redact_payload(value["description"][:500])
        normalized["description"] = "External field description omitted by security policy." if detect_prompt_injection(str(description)) else description
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
            properties[name] = _normalize_mcp_schema(child, depth + 1)
        normalized["properties"] = properties
        if isinstance(value.get("required"), list):
            normalized["required"] = [str(item)[:100] for item in value["required"][:64] if str(item)[:100] in properties]
    if isinstance(value.get("items"), dict):
        normalized["items"] = _normalize_mcp_schema(value["items"], depth + 1)
    normalized["additionalProperties"] = False if normalized.get("type") == "object" else value.get("additionalProperties", False)
    if depth == 0:
        normalized.setdefault("type", "object")
        normalized.setdefault("properties", {})
    return normalized


def _server_set_key(servers: list[dict[str, Any]], allow_local: bool) -> str:
    payload = json.dumps({"allow_local": allow_local, "servers": servers}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MCPConnectionManager:
    """Caches discovered MCP sessions and exposes credential-free lifecycle state."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def discover(
        self,
        servers: list[dict[str, Any]],
        allow_local: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[dict[str, Any], str]]]:
        key = _server_set_key(servers, allow_local)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and float(cached["expires_at"]) > now:
            return list(cached["definitions"]), dict(cached["routes"])
        async with self._lock:
            cached = self._cache.get(key)
            if cached and float(cached["expires_at"]) > time.monotonic():
                return list(cached["definitions"]), dict(cached["routes"])
            definitions, routes = await _discover_mcp_tools_uncached(servers, allow_local, cache_key=key)
            stamp = time.monotonic()
            self._cache[key] = {
                "definitions": definitions,
                "routes": routes,
                "created_at": stamp,
                "expires_at": stamp + self.ttl_seconds,
                "server_count": len(servers),
            }
            self._prune(stamp)
            return list(definitions), dict(routes)

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        active = [entry for entry in self._cache.values() if float(entry["expires_at"]) > now]
        return {
            "active_session_sets": len(active),
            "cached_tools": sum(len(entry["definitions"]) for entry in active),
            "ttl_seconds": self.ttl_seconds,
        }

    def _prune(self, now: float) -> None:
        for key in [key for key, entry in self._cache.items() if float(entry["expires_at"]) <= now]:
            self._cache.pop(key, None)


MCP_CONNECTIONS = MCPConnectionManager()


async def _discover_mcp_tools_uncached(
    servers: list[dict[str, Any]],
    allow_local: bool,
    *,
    cache_key: str,
) -> tuple[list[dict[str, Any]], dict[str, tuple[dict[str, Any], str]]]:
    definitions: list[dict[str, Any]] = []
    routes: dict[str, tuple[dict[str, Any], str]] = {}
    for server in servers:
        try:
            if server["transport"] in {"http", "sse"}:
                session_id, response = await initialize_http_mcp(server["url"], allow_local)
                server = {**server, "_session_id": session_id, "_session_cache_key": cache_key}
            elif allow_local:
                response = await call_stdio_mcp_async(server["command"], json.loads(server.get("args") or "[]"), "tools/list", {})
            else:
                continue
            for tool in response.get("result", {}).get("tools", []):
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    continue
                tool_name = tool["name"][:100]
                name = mcp_function_name(server["id"], tool_name)
                raw_description = str(tool.get("description") or tool_name)[:1000]
                safe_description, _ = redact_payload(raw_description)
                findings = detect_prompt_injection(str(safe_description))
                description = (
                    f"External MCP tool {tool_name}. Untrusted description omitted by security policy."
                    if findings
                    else f"MCP {str(server['name'])[:100]}: {safe_description[:500]}"
                )
                definitions.append({"type": "function", "function": {"name": name, "description": description, "parameters": _normalize_mcp_schema(tool.get("inputSchema") or {})}})
                routes[name] = (server, tool_name)
        except Exception:
            continue
    return definitions, routes


async def discover_mcp_tools(servers: list[dict[str, Any]], allow_local: bool) -> tuple[list[dict[str, Any]], dict[str, tuple[dict[str, Any], str]]]:
    return await MCP_CONNECTIONS.discover(servers, allow_local)


async def invoke_mcp_route(route: tuple[dict[str, Any], str], arguments: dict[str, Any], allow_local: bool) -> dict[str, Any]:
    server, tool_name = route
    params = {"name": tool_name, "arguments": arguments}
    if server["transport"] in {"http", "sse"}:
        try:
            return await call_http_mcp(server["url"], "tools/call", params, server.get("_session_id"), allow_local)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {404, 409, 410} or not server.get("_session_id"):
                raise
            MCP_CONNECTIONS.invalidate(str(server.get("_session_cache_key") or ""))
            session_id, _ = await initialize_http_mcp(server["url"], allow_local)
            server["_session_id"] = session_id
            return await call_http_mcp(server["url"], "tools/call", params, session_id, allow_local)
    if allow_local:
        return await call_stdio_mcp_async(server["command"], json.loads(server.get("args") or "[]"), "tools/call", params)
    raise ValueError("stdio MCP 仅在桌面本地后端显式启用")
