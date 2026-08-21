from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.permissions import PermissionDecision
from app.providers.provider import ProviderError
from app.runtime.runner import _filter_local_only_mcp_servers, _filter_local_only_tools
from app.tools.runtime_tools import execute_runtime_tool
from app.vision.service import _provider_for_mode


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def _runtime_kwargs(name: str, *, mcp_routes: dict | None = None) -> dict:
    return {
        "workspace": "",
        "mode": "full",
        "name": name,
        "arguments": {"query": "今天的新闻"} if name == "web_search" else {},
        "tool_call_id": "offline-call",
        "approved_actions": [],
        "approval_scope": "once",
        "conversation_id": 1,
        "task_id": "offline-task",
        "mcp_routes": mcp_routes or {},
        "allow_local_mcp": True,
    }


def test_runner_hides_external_tools_and_remote_mcp_before_discovery() -> None:
    extension_routes = {"extension.search": SimpleNamespace(delegate="web_search")}
    tools = [_tool("read_file"), _tool("web_search"), _tool("web_fetch"), _tool("vision.remote"), _tool("vision.describe"), _tool("extension.search")]
    assert [item["function"]["name"] for item in _filter_local_only_tools(tools, extension_routes)] == ["read_file", "vision.describe"]

    servers = [
        {"transport": "stdio", "command": "local.exe"},
        {"transport": "http", "url": "http://localhost:8123/mcp"},
        {"transport": "http", "url": "https://mcp.example/mcp"},
    ]
    assert _filter_local_only_mcp_servers(servers) == servers[:2]


def test_tool_boundary_blocks_builtin_and_remote_mcp_in_local_only_mode(monkeypatch) -> None:
    monkeypatch.setattr("app.tools.runtime_tools.local_only_policy", lambda: SimpleNamespace(enabled=True))
    web = asyncio.run(execute_runtime_tool(**_runtime_kwargs("web_search")))
    assert web.result["error_code"] == "offline_network_blocked"
    assert web.source == "local_only_policy"

    route = ({"transport": "http", "url": "https://mcp.example/mcp"}, "send")
    mcp = asyncio.run(execute_runtime_tool(**_runtime_kwargs("mcp_1_send", mcp_routes={"mcp_1_send": route})))
    assert mcp.result["error_code"] == "offline_network_blocked"


def test_non_local_provider_keeps_existing_web_search_path(monkeypatch) -> None:
    async def fake_search(query: str, **_kwargs):
        return {"success": True, "provider": "test", "query": query, "result_count": 0}

    monkeypatch.setattr("app.tools.runtime_tools.local_only_policy", lambda: SimpleNamespace(enabled=False))
    monkeypatch.setattr("app.tools.runtime_tools.search_web", fake_search)
    monkeypatch.setattr("app.tools.runtime_tools.emit_task_event", lambda *_args, **_kwargs: None)
    outcome = asyncio.run(
        execute_runtime_tool(
            **_runtime_kwargs("web_search"),
            permission_fn=lambda **_kwargs: PermissionDecision(True, True),
        )
    )
    assert outcome.result["success"] is True
    assert outcome.source == "builtin:web_search"


def test_remote_vision_is_blocked_while_ollama_local_only_is_active(monkeypatch) -> None:
    monkeypatch.setattr("app.vision.service.local_only_policy", lambda: SimpleNamespace(enabled=True))
    with pytest.raises(ProviderError) as raised:
        _provider_for_mode("remote", "secret")
    assert raised.value.error_type == "network_policy"
