import asyncio
import json
import uuid
from dataclasses import replace

from app.database import connect, now_iso
from app.permissions import authorize
from app.plugins.contracts import PluginCall
from app.plugins.discovery import MAX_ACTIVE_TOOLS, activate_discovered_tools
from app.plugins.registry import PLUGIN_LIBRARY
from app.runtime.executor import ExecutorToolCall, LocalWindowsExecutor
from app.runtime.recovery import list_checkpoints, load_checkpoint
from app.runtime.runner import interrupt_running_tasks, run_chat
from app.schemas import ChatRequest
from app.tools.registry import BASE_TOOLS, filter_readonly_tools, select_model_tools


def discovery_call(workspace, query, scope=None):
    return PluginCall(
        str(workspace),
        "full",
        "discover_tools",
        {"query": query},
        "discover",
        [],
        "once",
        1,
        "",
        authorize,
        available_tool_names=scope,
    )


def test_discovery_only_returns_configured_allowed_tools_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.speech_enabled", False)
    result = asyncio.run(
        PLUGIN_LIBRARY.execute(
            discovery_call(tmp_path, "find_symbol", ("find_symbol", "discover_tools"))
        )
    )
    assert result.result["tool_names"] == ["find_symbol"]
    assert "input_schema" not in json.dumps(result.result)
    assert result.result["tools"][0]["plugin_id"] == "builtin.code"
    denied = asyncio.run(
        PLUGIN_LIBRARY.execute(discovery_call(tmp_path, "find_symbol", ("read_file",)))
    )
    assert denied.result["tool_names"] == []
    voice = asyncio.run(PLUGIN_LIBRARY.execute(discovery_call(tmp_path, "transcribe_audio")))
    assert voice.result["tool_names"] == []
    empty = asyncio.run(PLUGIN_LIBRARY.execute(discovery_call(tmp_path, " ")))
    assert empty.result["error_code"] == "invalid_arguments"


def test_readonly_discovery_cannot_load_writes_and_does_not_grant_approval(tmp_path):
    available = filter_readonly_tools(BASE_TOOLS)
    scope = tuple(item["function"]["name"] for item in available)
    result = asyncio.run(PLUGIN_LIBRARY.execute(discovery_call(tmp_path, "write_file", scope)))
    assert result.result["tool_names"] == []
    loaded = select_model_tools("解释", (), [])
    active, added = activate_discovered_tools(
        loaded,
        available,
        {
            "success": True,
            "tool_names": ["write_file", "mcp__untrusted", "find_symbol"],
            "tools": [{"function": {"name": "write_file", "parameters": {}}}],
        },
    )
    assert added == ["find_symbol"]
    assert "write_file" not in {item["function"]["name"] for item in active}
    assert active[-1] is next(
        item for item in available if item["function"]["name"] == "find_symbol"
    )


def test_discovery_accepts_task_bound_search_configuration_without_exposing_keys(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.config.settings.tavily_api_key", "")
    monkeypatch.setattr("app.config.settings.brave_api_key", "")
    call = replace(
        discovery_call(tmp_path, "web_search", ("web_search",)),
        search_credentials={"tavily": "test-only-temporary-search-key"},
    )
    result = asyncio.run(PLUGIN_LIBRARY.execute(call))
    assert result.result["tool_names"] == ["web_search"]
    assert "test-only-temporary-search-key" not in json.dumps(result.result)


def test_activation_is_bounded_and_idempotent():
    loaded = BASE_TOOLS[:MAX_ACTIVE_TOOLS]
    result = {
        "success": True,
        "tool_names": [item["function"]["name"] for item in BASE_TOOLS[MAX_ACTIVE_TOOLS:]],
    }
    active, added = activate_discovered_tools(loaded, BASE_TOOLS, result)
    assert len(active) == MAX_ACTIVE_TOOLS and not added
    active, added = activate_discovered_tools(
        BASE_TOOLS[:1], BASE_TOOLS, {"success": True, "tool_names": ["find_symbol", "find_symbol"]}
    )
    assert added == ["find_symbol"]
    assert (
        activate_discovered_tools(
            active, BASE_TOOLS, {"success": True, "tool_names": ["find_symbol"]}
        )[1]
        == []
    )


def test_executor_enforces_task_scope_before_hooks_or_dispatch(tmp_path, monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Denied tools must not reach hooks or execution")

    monkeypatch.setattr("app.runtime.executor.run_hooks", unexpected)
    call = ExecutorToolCall(
        str(tmp_path),
        "full",
        "write_file",
        {},
        "denied",
        [],
        "once",
        1,
        "",
        {},
        available_tool_names=("read_file",),
    )
    outcome = asyncio.run(LocalWindowsExecutor().execute_tool(call))
    assert outcome.result["error_code"] == "tool_scope_violation"
    assert outcome.receipt.error_code == "tool_scope_violation"


def test_runtime_discovers_tools_and_restores_selection_after_interruption(tmp_path):
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "discovery", str(tmp_path), "full", stamp, stamp),
        )
    ready = asyncio.Event()
    requests = 0

    async def scripted(messages, api_key=None, tools=None, **kwargs):
        nonlocal requests
        requests += 1
        names = {item["function"]["name"] for item in tools or []}
        if requests == 1:
            assert "discover_tools" in names and "find_symbol" not in names
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "lookup-symbols",
                        "type": "function",
                        "function": {
                            "name": "discover_tools",
                            "arguments": '{"query":"find_symbol"}',
                        },
                    }
                ],
            }
        assert "find_symbol" in names
        ready.set()
        await asyncio.Event().wait()

    async def resumed(messages, api_key=None, tools=None, **kwargs):
        assert "find_symbol" in {item["function"]["name"] for item in tools or []}
        return {"role": "assistant", "content": "已找到符号查询能力。"}

    async def scenario():
        request = ChatRequest(
            conversation_id=conversation_id,
            task_id=task_id,
            content="请介绍能力",
            orchestration_mode="single",
        )
        running = asyncio.create_task(run_chat(request, completion_fn=scripted))
        await asyncio.wait_for(ready.wait(), timeout=4)
        interrupt_running_tasks()
        interrupted = await asyncio.wait_for(running, timeout=4)
        final = await run_chat(request.model_copy(update={"resume": True}), completion_fn=resumed)
        return interrupted, final

    interrupted, final = asyncio.run(scenario())
    assert interrupted["task_status"] == "interrupted"
    assert final["task_status"] == "completed"
    assert any(
        "find_symbol" in load_checkpoint(task_id, item["sequence"])["state"]["selected_tool_names"]
        for item in list_checkpoints(task_id)
    )
    with connect() as db:
        runs = db.execute(
            "SELECT tool,output FROM tool_runs WHERE task_id=?", (task_id,)
        ).fetchall()
    assert len(runs) == 1 and runs[0]["tool"] == "discover_tools"
    assert json.loads(runs[0]["output"])["plugin"]["id"] == "builtin.discovery"
