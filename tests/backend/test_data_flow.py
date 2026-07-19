import asyncio
import uuid
from pathlib import Path

from app.database import audit, connect, now_iso, rows
from app.runtime_tools import execute_runtime_tool


def _scope(workspace: Path) -> tuple[int, str]:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as database:
        database.execute(
            "INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "flow", str(workspace), "full", stamp, stamp),
        )
        database.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "flow", stamp, stamp),
        )
    return conversation_id, task_id


def test_credentials_are_redacted_before_audit_log() -> None:
    secret = "sk-test_DO_NOT_USE_000000000000"
    audit(None, "security_test", "local", "ok", {"message": f"Bearer {secret}"})
    log = rows("SELECT details FROM audit_logs ORDER BY id DESC LIMIT 1")[0]["details"]
    flow = rows("SELECT * FROM data_flow_events WHERE sink='audit_log' ORDER BY id DESC LIMIT 1")[0]
    assert secret not in log
    assert flow["classification"] == "credential"
    assert flow["redactions"] >= 1


def test_mcp_credentials_are_blocked_before_external_invocation(tmp_path: Path, monkeypatch) -> None:
    conversation_id, task_id = _scope(tmp_path)
    called = False

    async def invoke(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"result": "unsafe"}

    monkeypatch.setattr("app.runtime_tools.invoke_mcp_route", invoke)
    name = "mcp__1__send"
    arguments = {"api_key": "sk-test_DO_NOT_USE_000000000000", "value": "test"}
    kwargs = {
        "workspace": str(tmp_path),
        "mode": "full",
        "name": name,
        "arguments": arguments,
        "tool_call_id": "call-1",
        "approval_scope": "once",
        "conversation_id": conversation_id,
        "task_id": task_id,
        "mcp_routes": {name: ({"transport": "http", "url": "https://mcp.example"}, "send")},
        "allow_local_mcp": False,
    }
    pending = asyncio.run(execute_runtime_tool(approved_actions=[], **kwargs))
    token = pending.result["approval_key"]
    blocked = asyncio.run(execute_runtime_tool(approved_actions=[token], **kwargs))

    assert blocked.result["error_code"] == "credential_flow_blocked"
    assert called is False
    event = rows("SELECT * FROM data_flow_events WHERE sink=? ORDER BY id DESC LIMIT 1", (f"mcp:{name}",))[0]
    assert event["allowed"] == 0
    with connect() as database:
        capability = database.execute("SELECT capabilities FROM approval_grants ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "sk-test_DO_NOT_USE_000000000000" not in capability
