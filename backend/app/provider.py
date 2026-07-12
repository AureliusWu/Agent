from __future__ import annotations

import time
from typing import Any

import httpx

from .config import settings


BASE_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "list_files", "description": "列出工作区内的目录内容", "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}}},
    {"type": "function", "function": {"name": "search_files", "description": "在工作区文件中搜索文本或按文件名查找", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string", "default": "."}, "glob": {"type": "string", "default": "*"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "读取工作区内的文本文件，可指定行范围", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "写入工作区内的文本文件", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "move_file", "description": "移动或重命名工作区内的文件", "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}}},
    {"type": "function", "function": {"name": "create_directory", "description": "创建工作区目录", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "delete_file", "description": "删除工作区内的单个文件", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "在工作区内运行一个程序。参数必须分开传递，不使用 shell。", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}, "cwd": {"type": "string", "default": "."}, "timeout": {"type": "integer", "minimum": 1, "maximum": 120}}, "required": ["command"]}}},
]


async def completion(
    messages: list[dict[str, Any]],
    api_key: str | None = None,
    *,
    tools: list[dict[str, Any]] | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    key = api_key or settings.deepseek_api_key
    if not key:
        raise ValueError("未配置模型 API Key")
    payload: dict[str, Any] = {
        "model": model or settings.model_name,
        "messages": messages,
        "temperature": 0.2,
    }
    if tools:
        payload.update({"tools": tools, "tool_choice": "auto"})
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        response = await client.post(
            (base_url or settings.model_base_url).rstrip("/") + "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]


async def provider_health(api_key: str | None = None) -> dict[str, Any]:
    key = api_key or settings.deepseek_api_key
    if not key:
        return {"status": "unconfigured", "latency_ms": None, "model": settings.model_name}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(
                settings.model_base_url.rstrip("/") + "/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            response.raise_for_status()
        return {"status": "ok", "latency_ms": round((time.perf_counter() - started) * 1000), "model": settings.model_name}
    except Exception as exc:
        return {"status": "error", "latency_ms": round((time.perf_counter() - started) * 1000), "model": settings.model_name, "error": str(exc)}
