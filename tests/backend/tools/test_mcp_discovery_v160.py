from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
import httpx

from app.mcp.discovery import McpDiscoveryError
from app.mcp.rpc import McpRouteRevokedError, McpRpcResponse, McpTransportError
from app.tools.mcp import MCPConnectionManager, _TransportPayload, invoke_mcp_route


SERVER = {"id": 160, "name": "paged-fixture", "transport": "http", "url": "https://mcp.example/rpc"}


def page(*names: str, cursor: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in names]}
    if cursor is not None:
        result["nextCursor"] = cursor
    return {"result": result}


class PagesTransport:
    def __init__(self, pages: list[dict[str, Any] | Exception]) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, Any]] = []
        self.initialized = True
        self.session_id = "paged-session"
        self.closes = 0
        self.authorization_check: Callable[[], None] | None = None
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def open(self) -> None:
        self.initialized = True

    async def request(self, method: str, params: dict[str, Any]) -> McpRpcResponse:
        assert method == "tools/list", "discovery must never invoke an external tool"
        if self.authorization_check is not None:
            self.authorization_check()
        self.calls.append(dict(params))
        if params and self.entered is not None:
            self.entered.set()
            assert self.release is not None
            await self.release.wait()
        payload = self.pages.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return McpRpcResponse.parse(payload)

    async def close(self) -> None:
        self.closes += 1
        self.initialized = False


def install_pages(monkeypatch: pytest.MonkeyPatch, transport: PagesTransport) -> None:
    async def initialize(_url: str, _allow_local: bool = False, **_kwargs: Any):
        response = await transport.request("tools/list", {})
        return transport.session_id, _TransportPayload(response.as_payload(), transport)

    monkeypatch.setattr("app.tools.mcp.initialize_http_mcp", initialize)


def test_three_page_http_discovery_publishes_all_tools_with_one_session(monkeypatch) -> None:
    transport = PagesTransport([page("first", cursor="second-page"), page("second", cursor="last-page"), page("third")])
    install_pages(monkeypatch, transport)

    async def scenario() -> None:
        manager = MCPConnectionManager()
        definitions, routes = await manager.discover([SERVER], False)
        assert len(definitions) == 3
        assert [route[1] for route in routes.values()] == ["first", "second", "third"]
        assert transport.calls == [{}, {"cursor": "second-page"}, {"cursor": "last-page"}]
        assert len({id(route[0]["_mcp_transport"]) for route in routes.values()}) == 1
        assert manager.status()["last_discovery_errors"] == []
        assert (await manager.discover([SERVER], False))[0] == definitions
        assert len(transport.calls) == 3, "complete discovery may be cached"
        await manager.aclose()

    asyncio.run(scenario())


def test_stdio_discovery_uses_same_bounded_pagination_contract(monkeypatch) -> None:
    transport = PagesTransport([page("first", cursor="2"), page("second", cursor="3"), page("third")])
    monkeypatch.setattr("app.tools.mcp.StdioMcpTransport", lambda *_args, **_kwargs: transport)

    async def scenario() -> None:
        manager = MCPConnectionManager()
        definitions, _ = await manager.discover([{**SERVER, "transport": "stdio", "command": "test-only", "args": []}], True)
        assert len(definitions) == 3
        assert transport.calls == [{}, {"cursor": "2"}, {"cursor": "3"}]
        await manager.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("pages", (
    [page("first", cursor="repeated"), page("second", cursor="repeated")],
    [page("first", cursor="2"), page("first")],
    [page("files/read", cursor="2"), page("files-read")],
    [page("first", cursor=7)],
    [page("first", cursor="")],
    [page("first", cursor="2"), {"result": {"tools": "not-an-array"}}],
    [page("first", cursor="2"), {"result": {"tools": [{"name": "bad", "inputSchema": []}]}}],
    [page("first", cursor="2"), {"result": {"tools": [{"name": "bad", "inputSchema": {"type": "object", "properties": []}}]}}],
    [page("first", cursor="2"), {"result": {"tools": [{"name": "bad", "inputSchema": {"type": "array"}}]}}],
    [page("first", cursor="2"), {"result": {"tools": [{"name": ""}]}}],
    [page("first", cursor="2"), {"result": {"tools": [None]}}],
    [page("first", cursor="2"), McpTransportError("synthetic page failure")],
), ids=("cursor-loop", "duplicate-name", "route-alias-collision", "invalid-cursor", "empty-cursor", "invalid-tools", "invalid-schema", "invalid-properties", "non-object-input", "empty-name", "invalid-tool", "transport-failure"))
def test_incomplete_or_invalid_server_never_publishes_first_page(monkeypatch, pages) -> None:
    transport = PagesTransport(pages)
    install_pages(monkeypatch, transport)

    async def scenario() -> None:
        manager = MCPConnectionManager()
        with pytest.raises(McpDiscoveryError):
            await manager.discover([SERVER], False)
        assert manager.status()["cached_tools"] == 0
        assert manager.status()["active_session_sets"] == 0
        assert manager.status()["last_discovery_errors"]
        assert transport.closes >= 1
        await manager.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("setting,value", (("MAX_DISCOVERY_PAGES", 2), ("MAX_DISCOVERY_TOOLS", 2), ("MAX_DISCOVERY_BYTES", 160)))
def test_aggregate_discovery_budgets_are_enforced(monkeypatch, setting: str, value: int) -> None:
    monkeypatch.setattr("app.mcp.discovery." + setting, value, raising=False)
    transport = PagesTransport([page("one", cursor="2"), page("two", cursor="3"), page("three")])
    install_pages(monkeypatch, transport)

    async def scenario() -> None:
        manager = MCPConnectionManager()
        with pytest.raises(McpDiscoveryError):
            await manager.discover([SERVER], False)
        assert manager.status()["last_discovery_errors"][0]["error_code"] == "mcp_discovery_limit"
        assert manager.status()["cached_tools"] == 0
        await manager.aclose()

    asyncio.run(scenario())


def test_initialization_shares_the_total_discovery_deadline(monkeypatch) -> None:
    monkeypatch.setattr("app.mcp.discovery.DISCOVERY_TIMEOUT_SECONDS", 0.01, raising=False)

    async def slow_initialize(*_args, **_kwargs):
        await asyncio.sleep(0.1)
        return None, page("too-late")

    monkeypatch.setattr("app.tools.mcp.initialize_http_mcp", slow_initialize)

    async def scenario() -> None:
        manager = MCPConnectionManager()
        with pytest.raises(McpDiscoveryError):
            await manager.discover([SERVER], False)
        assert manager.status()["last_discovery_errors"][0]["error_code"] == "mcp_discovery_limit"
        assert manager.status()["cached_tools"] == 0
        await manager.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt", ("revoke", "cancel"))
def test_mid_page_revocation_or_cancellation_cannot_publish_or_keep_lease(monkeypatch, interrupt: str) -> None:
    async def scenario() -> None:
        transport = PagesTransport([page("first", cursor="2"), page("second")])
        transport.entered, transport.release = asyncio.Event(), asyncio.Event()
        install_pages(monkeypatch, transport)
        manager = MCPConnectionManager()
        running = asyncio.create_task(manager.discover([SERVER], False))
        try:
            await asyncio.wait_for(transport.entered.wait(), timeout=0.3)
            if interrupt == "revoke":
                manager.invalidate()
                transport.release.set()
                with pytest.raises(McpRouteRevokedError):
                    await running
            else:
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            assert manager.status()["active_session_sets"] == 0
            assert manager.status()["cached_tools"] == 0
            assert not manager._leases, "interrupted discovery must retire its unpublished authority"
            assert transport.closes >= 1
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await manager.aclose()

    asyncio.run(scenario())


def test_explicit_refresh_revokes_all_previous_page_routes(monkeypatch) -> None:
    transport = PagesTransport([page("first", cursor="2"), page("second")])
    install_pages(monkeypatch, transport)

    async def scenario() -> None:
        manager = MCPConnectionManager()
        _, routes = await manager.discover([SERVER], False)
        assert len(routes) == 2
        manager.invalidate()
        for route in routes.values():
            with pytest.raises(McpRouteRevokedError):
                await invoke_mcp_route(route, {}, False)
        assert len(transport.calls) == 2
        await manager.aclose()

    asyncio.run(scenario())


def test_health_probe_can_discover_disabled_server_but_not_invoke_it(monkeypatch) -> None:
    transport = PagesTransport([page("first", cursor="2"), page("second")])
    install_pages(monkeypatch, transport)

    async def scenario() -> None:
        manager = MCPConnectionManager()
        definitions, routes = await manager.discover([{**SERVER, "enabled": 0}], False)
        assert len(definitions) == 2, "authorized management probes run before enabling a server"
        for route in routes.values():
            with pytest.raises(McpRouteRevokedError):
                await invoke_mcp_route(route, {}, False)
        assert len(transport.calls) == 2
        await manager.aclose()

    asyncio.run(scenario())


def test_pagination_does_not_mix_cursors_from_replaced_http_session(monkeypatch) -> None:
    class ReplacedSession(PagesTransport):
        async def request(self, method, params):
            response = await super().request(method, params)
            if params:
                self.session_id = "replacement-session"
            return response

    transport = ReplacedSession([page("first", cursor="2"), page("second")])
    install_pages(monkeypatch, transport)

    async def scenario() -> None:
        manager = MCPConnectionManager()
        with pytest.raises(McpDiscoveryError):
            await manager.discover([SERVER], False)
        assert manager.status()["cached_tools"] == 0
        await manager.aclose()

    asyncio.run(scenario())


def test_aggregate_budget_is_shared_across_servers_without_leaking_failed_page(monkeypatch) -> None:
    monkeypatch.setattr("app.mcp.discovery.MAX_DISCOVERY_TOOLS", 3)
    transports = [PagesTransport([page("one", "two")]), PagesTransport([page("three", "four")])]

    async def initialize(_url, _allow_local=False, **_kwargs):
        transport = transports[0] if _url == SERVER["url"] else transports[1]
        return transport.session_id, _TransportPayload((await transport.request("tools/list", {})).as_payload(), transport)

    monkeypatch.setattr("app.tools.mcp.initialize_http_mcp", initialize)

    async def scenario() -> None:
        manager = MCPConnectionManager()
        definitions, routes = await manager.discover([SERVER, {**SERVER, "id": 161, "url": "https://other.example/rpc"}], False)
        assert len(definitions) == 2
        assert {route[1] for route in routes.values()} == {"one", "two"}
        assert manager.status()["last_discovery_errors"][0]["server_id"] == 161
        assert transports[1].closes >= 1
        await manager.aclose()

    asyncio.run(scenario())


def test_http_expired_session_rechecks_later_page_tool_before_retry(monkeypatch) -> None:
    from app.mcp.transports.http import HttpMcpTransport

    sessions: list[str] = []
    calls: list[str] = []
    list_requests: list[tuple[str, dict[str, Any]]] = []

    async def guarded(_client, method, url, **kwargs):
        payload = kwargs.get("json") or {}
        rpc_method = payload.get("method")
        session = kwargs.get("headers", {}).get("Mcp-Session-Id")
        request = httpx.Request(method, url)
        if method == "DELETE" or rpc_method == "notifications/initialized":
            return httpx.Response(202, request=request)
        if rpc_method == "initialize":
            sessions.append(f"session-{len(sessions) + 1}")
            return httpx.Response(200, headers={"Mcp-Session-Id": sessions[-1]}, json={"id": payload["id"], "result": {"capabilities": {}}}, request=request)
        if rpc_method == "tools/call":
            calls.append(session)
            if len(calls) == 1:
                return httpx.Response(404, request=request)
            result = {"content": [{"type": "text", "text": "safe synthetic result"}]}
        else:
            assert rpc_method == "tools/list"
            params = payload.get("params", {})
            list_requests.append((session, params))
            result = page("target")["result"] if params else page("other", cursor="next")["result"]
        return httpx.Response(200, json={"id": payload["id"], "result": result}, request=request)

    monkeypatch.setattr("app.mcp.transports.http.guarded_request", guarded)

    async def scenario() -> None:
        transport = HttpMcpTransport(SERVER["url"])
        try:
            response = await transport.request("tools/call", {"name": "target", "arguments": {}})
            assert response.result["content"]
        finally:
            await transport.close()

    asyncio.run(scenario())
    assert calls == ["session-1", "session-2"]
    assert list_requests == [("session-2", {}), ("session-2", {"cursor": "next"})]
