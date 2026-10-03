"""Cloud capability additions must preserve the v16 desktop boundaries."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes import tools
from app.permissions import PermissionDecision
from app.sandbox import file_version_token, write_uploaded_file
from app.schemas import ToolRequest


@pytest.mark.parametrize(
    "name",
    [
        "list_files", "create_file", "file_batch", "undo_file_batch",
        "remember_workspace", "artifact.create_text", "discover_tools",
        "transcribe_audio", "synthesize_speech",
    ],
)
def test_tool_api_uses_composed_executor_for_every_family(name, tmp_path, monkeypatch):
    calls = []
    audits = []
    permission_policy = SimpleNamespace(authorize=lambda **kwargs: PermissionDecision(True, True))

    class Executor:
        async def execute_tool(self, call):
            calls.append(call)
            return SimpleNamespace(result={"success": True, "status": "ok", "from": "composed"})

    monkeypatch.setattr(tools, "build_kernel_services", lambda: SimpleNamespace(
        executor=Executor(), permissions=permission_policy,
    ))
    monkeypatch.setattr(tools, "require_conversation_scope", lambda *args, **kwargs: SimpleNamespace(
        conversation_id=17, workspace=str(tmp_path), permission_mode="ask",
    ))
    monkeypatch.setattr(tools, "require_task_scope", lambda conversation_id, task_id: None)
    monkeypatch.setattr(tools, "audit", lambda *args: audits.append(args))
    payload = ToolRequest(
        conversation_id=17, workspace=str(tmp_path), permission_mode="ask",
        tool=name, arguments={"path": "input.txt"}, task_id="test-task",
        approval_tokens=["synthetic-approval"], approval_scope="task",
    )
    result = asyncio.run(tools.run_tool(payload))
    assert result["from"] == "composed" and len(calls) == 1
    call = calls[0]
    assert call.name == name and call.arguments == payload.arguments
    assert call.permission_fn is permission_policy.authorize
    assert call.conversation_id == 17 and call.task_id == "test-task"
    assert call.workspace == str(tmp_path) and call.mode == "ask"
    assert call.approved_actions == ["synthetic-approval"] and call.approval_scope == "task"
    assert call.memory_write_explicit and call.mcp_routes == {}
    assert call.tool_call_id.startswith("api:") and len(audits) == 1


def test_tool_api_refuses_scope_before_composing_executor(tmp_path, monkeypatch):
    def deny_scope(*args, **kwargs):
        raise HTTPException(409, "scope mismatch")

    def unexpected_executor():
        pytest.fail("scope rejection must happen before composing an executor")

    monkeypatch.setattr(tools, "require_conversation_scope", deny_scope)
    monkeypatch.setattr(tools, "build_kernel_services", unexpected_executor)
    payload = ToolRequest(conversation_id=17, workspace=str(tmp_path), tool="list_files")
    with pytest.raises(HTTPException) as failure:
        asyncio.run(tools.run_tool(payload))
    assert failure.value.status_code == 409


def test_all_v16_desktop_routes_coexist_with_cloud_plugin_catalog():
    from app.main import create_app

    paths = set(create_app().openapi()["paths"])
    assert {
        "/api/plugins", "/api/stt/health", "/api/tts/health", "/api/voice/events",
        "/api/vision/analyze", "/api/local-models/resources",
        "/api/file-recovery", "/api/file-transactions/{operation_id}/reconcile",
    } <= paths


def test_cloud_upload_version_guard_preserves_current_file(tmp_path):
    target = tmp_path / "reply.wav"
    target.write_bytes(b"existing")
    result = write_uploaded_file(
        str(tmp_path), "full", target.name, b"replacement",
        expected_version_token="missing",
        permission_fn=lambda **kwargs: PermissionDecision(True, True),
    )
    assert result["error_code"] == "version_conflict"
    assert target.read_bytes() == b"existing"
    assert not (tmp_path / ".agent-backups").exists()


def test_cloud_upload_still_obeys_injected_permission_policy(tmp_path):
    result = write_uploaded_file(
        str(tmp_path), "full", "reply.wav", b"replacement",
        permission_fn=lambda **kwargs: PermissionDecision(
            False, False, {"success": False, "status": "confirmation_required"},
        ),
    )
    assert result["status"] == "confirmation_required"
    assert not list(tmp_path.iterdir())


def test_cloud_upload_atomic_failure_restores_previous_content(tmp_path, monkeypatch):
    from app import sandbox

    target = tmp_path / "reply.wav"
    target.write_bytes(b"existing")
    original_replace = sandbox.os.replace

    def fail_commit(source, destination):
        if str(source).endswith(".tmp") and str(destination) == str(target):
            raise OSError("synthetic atomic commit failure")
        return original_replace(source, destination)

    monkeypatch.setattr(sandbox.os, "replace", fail_commit)
    result = write_uploaded_file(
        str(tmp_path), "full", target.name, b"replacement",
        expected_version_token=file_version_token(target), operation="synthesize_speech",
        tool_call_id="synthetic-speech-commit",
        permission_fn=lambda **kwargs: PermissionDecision(True, True),
    )
    assert result["status"] == "error" and target.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".*.tmp"))


def test_mcp_facade_keeps_initialized_transport_and_secret_reference(monkeypatch):
    from app.tools import mcp

    events = []

    class Response:
        def as_payload(self):
            return {"jsonrpc": "2.0", "result": {"tools": []}}

    class Transport:
        def __init__(self, command, args, *, secret_binding):
            events.append(("construct", command, args, secret_binding))

        async def open(self):
            events.append(("initialize",))
            return Response()

        async def request(self, method, params):
            events.append(("request", method, params))
            return Response()

        async def close(self):
            events.append(("close",))

    monkeypatch.setattr(mcp, "StdioMcpTransport", Transport)
    result = asyncio.run(mcp.call_stdio_mcp_async(
        "synthetic-mcp", [], "tools/list", {}, secret_binding="env:SYNTHETIC_MCP_TOKEN",
    ))
    assert result["result"]["tools"] == []
    assert events == [
        ("construct", "synthetic-mcp", [], "env:SYNTHETIC_MCP_TOKEN"),
        ("initialize",), ("request", "tools/list", {}), ("close",),
    ]
