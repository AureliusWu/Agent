from __future__ import annotations

import asyncio
import json
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from app.mcp.rpc import McpError, McpRpcResponse
from app.tools.mcp import MCPConnectionManager, _TransportPayload, invoke_mcp_route


SERVER = {"id": 157, "name": "fixture", "transport": "http", "url": "https://mcp.example/rpc"}


class FixtureTransport:
    def __init__(self) -> None:
        self.calls = 0
        self.closes = 0
        self.initialized = True
        self.allow_private = False
        self.session_id = "fixture"

    async def request(self, method, params):
        self.calls += 1
        return McpRpcResponse(2, result={"content": [{"type": "text", "text": "ok"}]})

    async def close(self):
        self.closes += 1


def install_discovery(monkeypatch, transport, *, tools=None, entered=None, release=None):
    async def initialize(_url, _allow_private=False, **_kwargs):
        if entered is not None:
            entered.set()
            await release.wait()
        return "fixture", _TransportPayload(
            {"result": {"tools": tools if tools is not None else [{"name": "echo"}]}}, transport,
        )

    monkeypatch.setattr("app.tools.mcp.initialize_http_mcp", initialize)


def test_revoked_cached_route_cannot_reopen_transport(monkeypatch) -> None:
    transport = FixtureTransport()
    install_discovery(monkeypatch, transport)

    async def scenario():
        manager = MCPConnectionManager()
        _, routes = await manager.discover([SERVER], False)
        route = next(iter(routes.values()))
        assert (await invoke_mcp_route(route, {}, False))["result"]
        manager.invalidate()
        with pytest.raises(McpError):
            await invoke_mcp_route(route, {}, False)
        await manager.aclose()
        assert transport.calls == 1
        assert transport.closes >= 1

    asyncio.run(scenario())


def test_empty_discovery_still_owns_and_closes_session(monkeypatch) -> None:
    transport = FixtureTransport()
    install_discovery(monkeypatch, transport, tools=[])

    async def scenario():
        manager = MCPConnectionManager()
        assert await manager.discover([SERVER], False) == ([], {})
        await manager.aclose()
        assert transport.closes >= 1

    asyncio.run(scenario())


def test_invalidation_during_discovery_cannot_publish_stale_routes(monkeypatch) -> None:
    async def scenario():
        manager = MCPConnectionManager()
        transport = FixtureTransport()
        entered, release = asyncio.Event(), asyncio.Event()
        install_discovery(monkeypatch, transport, entered=entered, release=release)
        pending = asyncio.create_task(manager.discover([SERVER], False))
        await entered.wait()
        manager.invalidate()
        release.set()
        with pytest.raises(McpError):
            await pending
        await manager.aclose()
        assert manager.status()["active_session_sets"] == 0
        assert transport.closes >= 1

    asyncio.run(scenario())


def test_cached_stdio_route_rechecks_local_policy() -> None:
    transport = FixtureTransport()
    route = ({"transport": "stdio", "command": "fixture", "args": [], "_mcp_transport": transport}, "echo")
    with pytest.raises((McpError, PermissionError)):
        asyncio.run(invoke_mcp_route(route, {}, False))
    assert transport.calls == 0


def test_http_session_compatibility_path_rejects_tool_is_error(monkeypatch) -> None:
    from app.tools.mcp import call_http_mcp

    async def guarded(_client, method, url, **kwargs):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"isError": True}}, request=httpx.Request(method, url))

    monkeypatch.setattr("app.tools.mcp.guarded_request", guarded)
    monkeypatch.setattr("app.mcp.transports.http.guarded_request", guarded)
    with pytest.raises(McpError) as failure:
        asyncio.run(call_http_mcp(SERVER["url"], "tools/call", {}, "existing"))
    assert failure.value.error_code == "mcp_tool_error"


def test_sync_stdio_rejects_tool_is_error_and_isolates_environment(monkeypatch) -> None:
    from app.tools.mcp import call_stdio_mcp

    captured = {}
    monkeypatch.setenv("SIYI_MCP_HOST_SECRET", "fixture-host-value")

    def run(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": 1, "result": {"isError": True}}), stderr="")

    monkeypatch.setattr("app.tools.mcp.subprocess.run", run)
    with pytest.raises(McpError) as failure:
        call_stdio_mcp("fixture", [], "tools/call", {})
    assert failure.value.error_code == "mcp_tool_error"
    assert "SIYI_MCP_HOST_SECRET" not in captured["env"]


@pytest.mark.parametrize("argument", ["--token=fixture", "--api-key=abc", "--password=abc", "ACCESS_TOKEN=fixture", "/password:fixture", "--accessToken=abc", "--clientSecret=abc", "--APIToken=abc", "--url=https://mcp.example?token=abc"])
def test_stdio_binding_rejects_equivalent_plaintext_flags(argument) -> None:
    from app.mcp.permissions import McpSecretBindingError, validate_stdio_server_binding

    with pytest.raises(McpSecretBindingError):
        validate_stdio_server_binding("fixture", [argument])


def test_http_status_failure_is_typed_and_does_not_replay(monkeypatch) -> None:
    from app.mcp.transports.http import HttpMcpTransport

    calls = []

    async def guarded(_client, method, url, **kwargs):
        payload = kwargs.get("json") or {}
        calls.append(payload.get("method"))
        status = 500 if payload.get("method") == "tools/call" else 200
        result = {"protocolVersion": "2025-03-26", "capabilities": {}}
        return httpx.Response(status, json={"id": payload.get("id"), "result": result}, request=httpx.Request(method, url))

    monkeypatch.setattr("app.mcp.transports.http.guarded_request", guarded)

    async def scenario():
        transport = HttpMcpTransport(SERVER["url"])
        try:
            with pytest.raises(McpError) as failure:
                await transport.request("tools/call", {"name": "echo"})
            assert failure.value.error_code == "mcp_transport_error"
            assert calls.count("tools/call") == 1
        finally:
            await transport.close()

    asyncio.run(scenario())


def test_queued_discovery_cannot_create_new_lease_after_invalidate(monkeypatch):
    async def scenario():
        manager = MCPConnectionManager()
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def initialize(_url, _allow_private=False):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await release.wait()
            return None, {"result": {"tools": [{"name": "echo"}]}}

        monkeypatch.setattr("app.tools.mcp.initialize_http_mcp", initialize)
        first = asyncio.create_task(manager.discover([SERVER], False))
        await entered.wait()
        second = asyncio.create_task(manager.discover([{**SERVER, "id": 158}], False))
        await asyncio.sleep(0)
        manager.invalidate()
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, McpError) for result in results)
        assert count == 1
        await manager.aclose()

    asyncio.run(scenario())


def test_discovery_passes_secret_reference_not_secret_value(monkeypatch) -> None:
    captured = []

    async def initialize(_url, _allow_private=False, *, secret_binding=None):
        captured.append(secret_binding)
        return None, {"result": {"tools": []}}

    monkeypatch.setattr("app.tools.mcp.initialize_http_mcp", initialize)

    async def scenario():
        manager = MCPConnectionManager()
        await manager.discover([{**SERVER, "secret_binding": "env:SIYI_MCP_FIXTURE"}], False)
        await manager.aclose()

    asyncio.run(scenario())
    assert captured == ["env:SIYI_MCP_FIXTURE"]


def test_http_404_reinitializes_owned_session_and_closes_replacement(monkeypatch) -> None:
    sessions, calls, closes = [], [], []

    async def guarded(_client, method, url, **kwargs):
        payload = kwargs.get("json") or {}
        headers = kwargs.get("headers") or {}
        request = httpx.Request(method, url)
        if method == "DELETE":
            closes.append(headers["Mcp-Session-Id"])
            return httpx.Response(204, request=request)
        rpc_method = payload.get("method")
        if rpc_method == "notifications/initialized":
            return httpx.Response(202, request=request)
        if rpc_method == "initialize":
            session = f"session-{len(sessions) + 1}"
            sessions.append(session)
            return httpx.Response(200, headers={"Mcp-Session-Id": session}, json={"id": payload["id"], "result": {"protocolVersion": "2025-03-26", "capabilities": {}}}, request=request)
        if rpc_method == "tools/call":
            calls.append(headers["Mcp-Session-Id"])
            if len(calls) == 1:
                return httpx.Response(404, request=request)
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            result = {"tools": [{"name": "echo"}]}
        return httpx.Response(200, json={"id": payload["id"], "result": result}, request=request)

    monkeypatch.setattr("app.mcp.transports.http.guarded_request", guarded)

    async def scenario():
        manager = MCPConnectionManager()
        _, routes = await manager.discover([SERVER], False)
        result = await invoke_mcp_route(next(iter(routes.values())), {}, False)
        assert result["result"]["content"][0]["text"] == "ok"
        await manager.aclose()

    asyncio.run(scenario())
    assert sessions == calls == ["session-1", "session-2"]
    assert "session-2" in closes


def test_http_sse_selects_matching_response_after_notifications_and_batch():
    from app.mcp.transports.http import _response_payload

    body = 'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\ndata: [{"id":8,"result":{}},{"id":9,"error":{"code":-32602,"message":"bad"}}]\n\n'
    response = httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})
    parsed = McpRpcResponse.parse(_response_payload(response, expected_id=9), expected_id=9)
    with pytest.raises(McpError):
        parsed.raise_for_error()


def test_explicit_bound_secret_is_redacted_if_server_echoes_it(monkeypatch):
    from app.mcp.transports.http import HttpMcpTransport

    sentinel = "fixture-bound-value"
    monkeypatch.setenv("SIYI_MCP_FIXTURE", sentinel)

    async def guarded(_client, method, url, **kwargs):
        payload = kwargs.get("json") or {}
        assert kwargs["headers"]["Authorization"] == f"Bearer {sentinel}"
        result = {"capabilities": {}} if payload.get("method") == "initialize" else {"content": [{"type": "text", "text": sentinel}]}
        return httpx.Response(200, json={"id": payload.get("id"), "result": result}, request=httpx.Request(method, url))

    monkeypatch.setattr("app.mcp.transports.http.guarded_request", guarded)

    async def scenario():
        transport = HttpMcpTransport(SERVER["url"], secret_binding="env:SIYI_MCP_FIXTURE")
        try:
            result = await transport.request("tools/call", {"name": "echo"})
            assert sentinel not in json.dumps(result.as_payload())
        finally:
            await transport.close()

    asyncio.run(scenario())


def test_stdio_notification_flood_cannot_extend_total_deadline(tmp_path):
    from app.mcp.transports.stdio import StdioMcpTransport

    server = tmp_path / "notifications.py"
    server.write_text(
        "import sys,json,time\n"
        "for line in sys.stdin:\n"
        " p=json.loads(line)\n"
        " if p.get('method')=='initialize':\n"
        "  print(json.dumps({'id':p['id'],'result':{'capabilities':{}}}),flush=True)\n"
        " elif p.get('method')=='tools/call':\n"
        "  for _ in range(200):\n"
        "   print(json.dumps({'method':'notifications/progress','params':{}}),flush=True)\n"
        "   time.sleep(0.01)\n", encoding="utf-8",
    )

    async def scenario():
        transport = StdioMcpTransport(sys.executable, [str(server)], timeout=3)
        await transport.open()
        transport.timeout = 0.1
        started = time.monotonic()
        with pytest.raises(McpError):
            await transport.request("tools/call", {"name": "slow"})
        assert time.monotonic() - started < 1.5
        assert transport.pid is None
        await transport.close()

    asyncio.run(scenario())


def test_malformed_jsonrpc_version_is_a_typed_failure():
    with pytest.raises(McpError):
        McpRpcResponse.parse({"jsonrpc": {}, "id": 1, "result": {}}, expected_id=1)


def test_initialized_http_discovery_failure_terminates_remote_session(monkeypatch):
    from app.tools.mcp import initialize_http_mcp

    methods = []

    async def guarded(_client, method, url, **kwargs):
        payload = kwargs.get("json") or {}
        methods.append(payload.get("method", method))
        request = httpx.Request(method, url)
        if method == "DELETE" or payload.get("method") == "notifications/initialized":
            return httpx.Response(202, request=request)
        if payload.get("method") == "tools/list":
            return httpx.Response(200, json={"id": payload["id"], "error": {"code": -32603, "message": "failure"}}, request=request)
        return httpx.Response(200, headers={"Mcp-Session-Id": "allocated"}, json={"id": payload["id"], "result": {"capabilities": {}}}, request=request)

    monkeypatch.setattr("app.mcp.transports.http.guarded_request", guarded)
    with pytest.raises(McpError):
        asyncio.run(initialize_http_mcp(SERVER["url"]))
    assert methods == ["initialize", "notifications/initialized", "tools/list", "DELETE"]


def test_stdio_accepts_bounded_response_above_default_streamreader_limit(tmp_path):
    from app.mcp.transports.stdio import StdioMcpTransport

    server = tmp_path / "long_response.py"
    server.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " p=json.loads(line)\n"
        " if 'id' not in p: continue\n"
        " result={'capabilities':{}} if p.get('method')=='initialize' else {'content':[{'type':'text','text':'x'*70000}]}\n"
        " print(json.dumps({'id':p['id'],'result':result}),flush=True)\n", encoding="utf-8",
    )

    async def scenario():
        transport = StdioMcpTransport(sys.executable, [str(server)])
        try:
            result = await transport.request("tools/call", {"name": "echo"})
            assert len(result.result["content"][0]["text"]) == 70000
        finally:
            await transport.close()

    asyncio.run(scenario())


def test_stdio_normalizes_binding_before_redacting_echo(tmp_path, monkeypatch):
    from app.mcp.transports.stdio import StdioMcpTransport

    sentinel = "fixture-bound-value"
    monkeypatch.setenv("SIYI_MCP_FIXTURE", sentinel)
    server = tmp_path / "echo_binding.py"
    server.write_text(
        "import json,sys,os\n"
        "for line in sys.stdin:\n"
        " p=json.loads(line)\n"
        " if 'id' not in p: continue\n"
        " result={'capabilities':{}} if p.get('method')=='initialize' else {'content':[{'type':'text','text':os.environ.get('SIYI_MCP_FIXTURE','missing')}]}\n"
        " print(json.dumps({'id':p['id'],'result':result}),flush=True)\n", encoding="utf-8",
    )

    async def scenario():
        transport = StdioMcpTransport(sys.executable, [str(server)], secret_binding=" env:SIYI_MCP_FIXTURE ")
        try:
            result = await transport.request("tools/call", {"name": "echo"})
            assert result.result["content"][0]["text"] == "[REDACTED]"
        finally:
            await transport.close()

    asyncio.run(scenario())
