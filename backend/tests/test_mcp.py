import asyncio
import json
import sys
from types import SimpleNamespace

from app.mcp import call_stdio_mcp, call_stdio_mcp_async, mcp_function_name


def test_mcp_function_name_is_provider_safe() -> None:
    assert mcp_function_name(7, "files/read-item") == "mcp__7__files_read_item"


def test_stdio_mcp_uses_json_rpc_result(monkeypatch) -> None:
    response = SimpleNamespace(returncode=0, stdout=json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}), stderr="")
    monkeypatch.setattr("app.mcp.subprocess.run", lambda *args, **kwargs: response)
    assert call_stdio_mcp("mcp", [], "tools/list", {})["result"]["ok"] is True


def test_stdio_mcp_process_is_terminated_when_cancelled() -> None:
    async def scenario() -> None:
        running = asyncio.create_task(call_stdio_mcp_async(sys.executable, ["-c", "import sys,time; sys.stdin.readline(); time.sleep(30)"], "tools/list", {}))
        await asyncio.sleep(0.3)
        running.cancel()
        try:
            await asyncio.wait_for(running, timeout=3)
        except asyncio.CancelledError:
            return
        raise AssertionError("stdio MCP task was not cancelled")

    asyncio.run(scenario())
