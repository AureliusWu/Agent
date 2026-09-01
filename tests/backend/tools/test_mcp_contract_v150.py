from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient

from app.database import connect, now_iso, rows
from app.main import app
from app.permissions import PermissionDecision
from app.tools.runtime_tools import execute_runtime_tool


@pytest.fixture(autouse=True)
def _isolate_mcp_server_registry() -> Iterator[None]:
    """Do not leak MCP server fixtures into later Runner contract tests."""

    existing_ids = {int(item["id"]) for item in rows("SELECT id FROM mcp_servers")}
    yield
    created_ids = [
        int(item["id"])
        for item in rows("SELECT id FROM mcp_servers")
        if int(item["id"]) not in existing_ids
    ]
    if created_ids:
        with connect() as database:
            for server_id in created_ids:
                database.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))

    # Bypass any per-test monkeypatch of the instance method so teardown always
    # revokes routes and cached discovery results owned by this test.
    from app.mcp.session import MCPConnectionManager
    from app.tools.mcp import MCP_CONNECTIONS

    MCPConnectionManager.invalidate(MCP_CONNECTIONS)


def test_json_rpc_response_distinguishes_result_from_error() -> None:
    from app.mcp.rpc import McpRpcError, McpRpcResponse

    successful = McpRpcResponse.parse(
        {"jsonrpc": "2.0", "id": 4, "result": {"tools": []}},
        expected_id=4,
    )
    assert successful.success is True
    assert successful.result == {"tools": []}

    failed = McpRpcResponse.parse(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "error": {"code": -32602, "message": "bad api_key=fixture-private-value"},
        },
        expected_id=4,
    )
    assert failed.success is False
    with pytest.raises(McpRpcError) as raised:
        failed.raise_for_error()
    tool_failure = raised.value.to_tool_failure()
    assert tool_failure["success"] is False
    assert tool_failure["error_code"] == "mcp_jsonrpc_error"
    assert tool_failure["rpc_error_code"] == -32602
    assert "fixture-private-value" not in tool_failure["error_message"]


def test_legacy_stdio_facade_raises_typed_json_rpc_error(monkeypatch) -> None:
    from app.mcp.rpc import McpRpcError
    from app.tools.mcp import call_stdio_mcp

    response = SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method missing"}}
        ),
        stderr="",
    )
    monkeypatch.setattr("app.tools.mcp.subprocess.run", lambda *args, **kwargs: response)
    with pytest.raises(McpRpcError) as raised:
        call_stdio_mcp("mcp", [], "tools/list", {})
    assert raised.value.code == -32601


def test_stdio_transport_initializes_reuses_process_and_closes_cleanly(tmp_path: Path) -> None:
    from app.mcp.transports.stdio import StdioMcpTransport

    trace = tmp_path / "trace.jsonl"
    server = tmp_path / "server.py"
    server.write_text(
        """
import json
import pathlib
import sys

trace = pathlib.Path(sys.argv[1])
for line in sys.stdin:
    message = json.loads(line)
    with trace.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(message) + "\\n")
    method = message.get("method")
    if method == "initialize":
        response = {"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "fixture", "version": "1"}}}
    elif method == "tools/list":
        response = {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}}
    elif method == "tools/call":
        response = {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": "ok"}]}}
    else:
        continue
    print(json.dumps(response), flush=True)
""".strip()
        + "\n",
        encoding="utf-8",
    )

    async def scenario() -> None:
        transport = StdioMcpTransport(sys.executable, [str(server), str(trace)])
        await transport.open()
        listed = await transport.request("tools/list", {})
        called = await transport.request("tools/call", {"name": "echo", "arguments": {}})
        assert listed.result["tools"][0]["name"] == "echo"
        assert called.result["content"][0]["text"] == "ok"
        await transport.close()

    asyncio.run(scenario())
    messages = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    methods = [message.get("method") for message in messages]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]


def test_http_transport_preserves_session_header_and_terminates_session(monkeypatch) -> None:
    from app.mcp.transports.http import HttpMcpTransport

    calls: list[tuple[str, dict[str, str], dict | None]] = []

    async def guarded(_client, method, url, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        payload = kwargs.get("json")
        calls.append((method, headers, payload))
        request = httpx.Request(method, url)
        if method == "DELETE":
            return httpx.Response(204, request=request)
        rpc_method = payload.get("method")
        if rpc_method == "notifications/initialized":
            return httpx.Response(202, request=request)
        if rpc_method == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-15", "Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {"capabilities": {}}},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}},
            request=request,
        )

    monkeypatch.setattr("app.mcp.transports.http.guarded_request", guarded)

    async def scenario() -> None:
        transport = HttpMcpTransport("https://mcp.example/rpc")
        await transport.open()
        response = await transport.request("tools/list", {})
        assert response.result == {"tools": []}
        await transport.close()

    asyncio.run(scenario())
    assert [item[2].get("method") if item[2] else "DELETE" for item in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "DELETE",
    ]
    assert "Mcp-Session-Id" not in calls[0][1]
    assert all(item[1].get("Mcp-Session-Id") == "session-15" for item in calls[1:])


def test_discovery_does_not_silently_swallow_an_unreachable_server(monkeypatch) -> None:
    from app.mcp.discovery import McpDiscoveryError
    from app.tools.mcp import MCP_CONNECTIONS, discover_mcp_tools

    async def failed_initialize(_url: str, _allow_private: bool = False):
        raise RuntimeError("connection refused")

    MCP_CONNECTIONS.invalidate()
    monkeypatch.setattr("app.tools.mcp.initialize_http_mcp", failed_initialize)
    with pytest.raises(McpDiscoveryError, match="connection refused"):
        asyncio.run(
            discover_mcp_tools(
                [{"id": 99, "name": "broken", "transport": "http", "url": "https://mcp.invalid"}],
                False,
            )
        )
    status = MCP_CONNECTIONS.status()
    assert status["last_discovery_errors"]
    assert status["last_discovery_errors"][0]["server_id"] == 99


def test_runtime_turns_json_rpc_error_into_typed_tool_failure(tmp_path: Path, monkeypatch) -> None:
    from app.mcp.rpc import McpRpcError
    from app.runtime.executor import ExecutorToolCall, LocalWindowsExecutor

    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as database:
        database.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "mcp-contract", str(tmp_path), "full", stamp, stamp),
        )
        database.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "mcp", stamp, stamp),
        )

    async def invoke(*_args, **_kwargs):
        raise McpRpcError(-32602, "invalid parameters")

    monkeypatch.setattr("app.tools.runtime_tools.invoke_mcp_route", invoke)
    monkeypatch.setattr(
        "app.tools.runtime_tools.create_operation_checkpoint",
        lambda *_args, **_kwargs: {
            "id": "snapshot-1",
            "operation_scope": "external_mcp",
            "rollback_scope": "external_receipt_only",
            "rollback_paths": [],
        },
    )
    outcome = asyncio.run(
        execute_runtime_tool(
            workspace=str(tmp_path),
            mode="full",
            name="mcp__5__broken",
            arguments={"value": "safe"},
            tool_call_id="call-mcp-error",
            approved_actions=[],
            approval_scope="once",
            conversation_id=conversation_id,
            task_id=task_id,
            mcp_routes={"mcp__5__broken": ({"transport": "http", "url": "https://mcp.example"}, "broken")},
            allow_local_mcp=False,
            permission_fn=lambda **_kwargs: PermissionDecision(True, True),
        )
    )
    assert outcome.result == {
        "success": False,
        "status": "error",
        "error_code": "mcp_jsonrpc_error",
        "error_message": "invalid parameters",
        "rpc_error_code": -32602,
        "retryable": False,
        "security_snapshot_id": "snapshot-1",
        "operation_scope": "external_mcp",
        "rollback_scope": "external_receipt_only",
        "rollback_paths": [],
    }
    audit_entry = rows(
        "SELECT status,details FROM audit_logs WHERE action='mcp_call' AND conversation_id=? ORDER BY id DESC LIMIT 1",
        (conversation_id,),
    )[0]
    assert audit_entry["status"] == "error"
    assert "mcp_jsonrpc_error" in audit_entry["details"]

    completed = asyncio.run(
        LocalWindowsExecutor().execute_tool(
            ExecutorToolCall(
                workspace=str(tmp_path),
                mode="full",
                name="mcp__5__broken",
                arguments={"value": "safe"},
                tool_call_id="call-mcp-error-receipt",
                approved_actions=[],
                approval_scope="once",
                conversation_id=conversation_id,
                task_id=task_id,
                mcp_routes={"mcp__5__broken": ({"transport": "http", "url": "https://mcp.example"}, "broken")},
                allow_local_mcp=False,
                permission_fn=lambda **_kwargs: PermissionDecision(True, True),
            )
        )
    )
    assert completed.receipt is not None
    assert completed.receipt.success is False
    assert completed.receipt.standard_status == "FAILED"
    assert completed.receipt.error_code == "mcp_jsonrpc_error"
    assert completed.result["receipt"]["success"] is False


def test_mcp_uses_bounded_external_operation_checkpoint_for_large_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as database:
        database.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "mcp-checkpoint", str(tmp_path), "full", stamp, stamp),
        )
        database.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "mcp", stamp, stamp),
        )
    monkeypatch.setattr("app.workspace.snapshots.settings.security_snapshot_max_bytes", 1_000_000)
    (tmp_path / "ordinary-large-data.bin").write_bytes(b"x" * 1_000_001)

    async def invoke(*_args, **_kwargs):
        return {"value": "safe"}

    monkeypatch.setattr("app.tools.runtime_tools.invoke_mcp_route", invoke)
    outcome = asyncio.run(
        execute_runtime_tool(
            workspace=str(tmp_path),
            mode="full",
            name="mcp__5__safe",
            arguments={"value": "safe"},
            tool_call_id="call-mcp-checkpoint",
            approved_actions=[],
            approval_scope="once",
            conversation_id=conversation_id,
            task_id=task_id,
            mcp_routes={"mcp__5__safe": ({"transport": "http", "url": "https://mcp.example"}, "safe")},
            allow_local_mcp=False,
            permission_fn=lambda **_kwargs: PermissionDecision(True, True),
        )
    )

    assert outcome.result["success"] is True
    assert outcome.result["operation_scope"] == "external_mcp"
    assert outcome.result["rollback_scope"] == "external_receipt_only"
    snapshot_id = outcome.result["security_snapshot_id"]
    with connect() as database:
        record = database.execute(
            "SELECT manifest_path,file_count,total_bytes FROM security_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
    manifest = json.loads(Path(record["manifest_path"]).read_text(encoding="utf-8"))
    assert record["file_count"] == 0
    assert record["total_bytes"] == 0
    assert manifest["selection"]["mode"] == "operation_checkpoint"
    assert manifest["selection"]["operation_scope"] == "external_mcp"
    assert manifest["selection"]["rollback_scope"] == "external_receipt_only"
    from app.workspace.snapshots import SnapshotError, restore_security_snapshot

    with pytest.raises(SnapshotError, match="audit and receipt evidence only"):
        restore_security_snapshot(str(tmp_path), snapshot_id)


def test_transport_rejects_plaintext_secret_in_server_configuration() -> None:
    from app.mcp.permissions import (
        McpSecretBindingError,
        validate_http_server_binding,
        validate_stdio_server_binding,
    )

    with pytest.raises(McpSecretBindingError):
        validate_http_server_binding("https://mcp.example/rpc?api_key=fixture-private-value")
    with pytest.raises(McpSecretBindingError):
        validate_stdio_server_binding("mcp-server", ["--api-key", "plain-text-value"])
    # Ordinary names that merely contain the letters "key" are not credentials.
    validate_http_server_binding("https://mcp.example/rpc?hockey=enabled")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "unsafe-http",
            "transport": "http",
            "url": "https://mcp.example/rpc?api_key=plain-text-value",
            "command": None,
            "args": [],
        },
        {
            "name": "unsafe-stdio",
            "transport": "stdio",
            "url": None,
            "command": "mcp-server",
            "args": ["--api-key", "plain-text-value"],
        },
    ],
)
def test_management_rejects_plaintext_mcp_credentials_before_database_write(
    tmp_path: Path,
    payload: dict,
) -> None:
    with TestClient(app) as client:
        conversation = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "permission_mode": "ask"},
        ).json()
        ui_session = "mcp-secret-test-session"
        grant = client.post(
            "/api/admin-actions/grants",
            json={
                "operation": "mcp.register",
                "target_id": "new",
                "payload": payload,
                "ui_session_id": ui_session,
                "conversation_id": conversation["id"],
                "administrator_confirmed": True,
            },
        )
        assert grant.status_code == 200
        before = rows("SELECT id FROM mcp_servers WHERE name=?", (payload["name"],))
        response = client.post(
            "/api/mcp",
            json=payload,
            headers={
                "X-Siyi-Admin-Grant": grant.json()["grant_token"],
                "X-Siyi-UI-Session": ui_session,
                "X-Siyi-Conversation-Id": str(conversation["id"]),
            },
        )
        after = rows("SELECT id FROM mcp_servers WHERE name=?", (payload["name"],))

    assert before == []
    assert response.status_code == 400
    assert "密钥绑定" in response.json()["detail"]
    assert after == []


def test_mcp_management_mutation_invalidates_cached_sessions(tmp_path: Path, monkeypatch) -> None:
    invalidations: list[str] = []
    monkeypatch.setattr(
        "app.api.routes.extensions.MCP_CONNECTIONS.invalidate",
        lambda _key=None: invalidations.append("invalidated"),
    )
    stamp = now_iso()
    with connect() as database:
        server_id = int(
            database.execute(
                "INSERT INTO mcp_servers(name,transport,url,command,args,enabled,created_at) VALUES(?,?,?,?,?,?,?)",
                ("delete-session", "http", "https://mcp.example/rpc", None, "[]", 0, stamp),
            ).lastrowid
        )

    with TestClient(app) as client:
        conversation = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "permission_mode": "ask"},
        ).json()
        ui_session = "mcp-invalidate-session"
        grant = client.post(
            "/api/admin-actions/grants",
            json={
                "operation": "mcp.delete",
                "target_id": str(server_id),
                "payload": {},
                "ui_session_id": ui_session,
                "conversation_id": conversation["id"],
                "administrator_confirmed": True,
            },
        ).json()
        response = client.delete(
            f"/api/mcp/{server_id}",
            headers={
                "X-Siyi-Admin-Grant": grant["grant_token"],
                "X-Siyi-UI-Session": ui_session,
                "X-Siyi-Conversation-Id": str(conversation["id"]),
            },
        )
        assert invalidations == ["invalidated"]

    assert response.status_code == 200
    assert invalidations == ["invalidated", "invalidated"]  # mutation, then lifespan shutdown
    assert rows("SELECT id FROM mcp_servers WHERE id=?", (server_id,)) == []


def test_direct_mcp_api_uses_executor_failure_receipt(tmp_path: Path, monkeypatch) -> None:
    from app.mcp.rpc import McpRpcError

    calls = []

    async def failed(*_args, **_kwargs):
        calls.append("called")
        raise McpRpcError(-32602, "invalid parameters")

    monkeypatch.setattr("app.tools.runtime_tools.invoke_mcp_route", failed)
    monkeypatch.setattr("app.api.routes.extensions.call_http_mcp", failed, raising=False)
    with connect() as database:
        server_id = int(database.execute(
            "INSERT INTO mcp_servers(name,transport,url,enabled,created_at) VALUES(?,?,?,?,?)",
            ("direct-api", "http", "https://mcp.example/rpc", 1, now_iso()),
        ).lastrowid)
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        payload = {"server_id": server_id, "conversation_id": conversation["id"], "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "safe"}}}
        initial = client.post("/api/mcp/call", json=payload).json()
        assert initial["status"] == "confirmation_required"
        assert calls == []
        payload["approval_tokens"] = [initial["approval_key"]]
        response = client.post("/api/mcp/call", json=payload)
    assert response.status_code == 200
    failure = response.json()
    assert failure["error_code"] == "mcp_jsonrpc_error"
    assert failure["rpc_error_code"] == -32602
    assert failure["security_snapshot_id"]
    assert failure["receipt"]["success"] is False
    assert failure["receipt"]["standard_status"] == "FAILED"
    assert failure["receipt"]["risk_level"] == "critical"
    assert calls == ["called"]


def test_management_stores_only_optional_secret_reference(tmp_path: Path, monkeypatch) -> None:
    async def probe(_item):
        return [{"function": {"name": "fixture"}}], None

    async def validated(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.api.routes.extensions._probe_mcp", probe)
    monkeypatch.setattr("app.api.routes.extensions.validate_outbound_url", validated)
    payload = {"name": "bound-server", "transport": "http", "url": "https://mcp.example/rpc", "command": None, "args": [], "secret_binding": "env:SIYI_MCP_FIXTURE"}
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        ui_session = "mcp-binding-session"
        grant = client.post("/api/admin-actions/grants", json={"operation": "mcp.register", "target_id": "new", "payload": payload, "ui_session_id": ui_session, "conversation_id": conversation["id"], "administrator_confirmed": True}).json()
        response = client.post("/api/mcp", json=payload, headers={"X-Siyi-Admin-Grant": grant["grant_token"], "X-Siyi-UI-Session": ui_session, "X-Siyi-Conversation-Id": str(conversation["id"])})
    assert response.status_code == 200
    assert response.json()["secret_binding"] == "env:SIYI_MCP_FIXTURE"
    assert rows("SELECT secret_binding FROM mcp_servers WHERE id=?", (response.json()["id"],))[0]["secret_binding"] == "env:SIYI_MCP_FIXTURE"
