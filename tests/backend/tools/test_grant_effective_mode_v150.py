from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.database import connect, now_iso
from app.extensions.sdk import ExtensionToolRoute
from app.tools.file_operations import FileOperationRequest, execute_file_batch
from app.tools.runtime_tools import execute_runtime_tool


def conversation(workspace: Path, mode: str) -> int:
    with connect() as db:
        return db.execute("INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("grant scope fixture", str(workspace), mode, now_iso(), now_iso())).lastrowid


def execute(kind: str, workspace: Path, mode: str, conversation_id: int, tokens=None):
    arguments = {"path": "result.txt", "content": "approved", "expected_version_token": "missing"}
    if kind == "batch":
        return execute_file_batch(str(workspace), [FileOperationRequest("file.write", arguments)],
            mode=mode, conversation_id=conversation_id, approval_tokens=tokens or [])
    item = ExtensionToolRoute("ext__mode__write", "mode", "1.0.0", "write_file", "medium", {}, "fixture")
    return asyncio.run(execute_runtime_tool(workspace=str(workspace), mode=mode, name=item.name,
        arguments=arguments, tool_call_id="mode-fixture", approved_actions=tokens or [],
        approval_scope="once", conversation_id=conversation_id, task_id=None, mcp_routes={},
        extension_routes={item.name: item}, allow_local_mcp=False)).result


@pytest.mark.parametrize("kind", ["batch", "extension"])
@pytest.mark.parametrize("authority,effective,allowed", [
    ("full", "ask", True), ("agent", "ask", True),
    ("readonly", "ask", False), ("readonly", "full", False),
    ("ask", "full", False), ("agent", "full", False), ("full", "agent", False),
])
def test_only_explicit_ask_tightening_is_compatible(tmp_path: Path, kind: str, authority: str,
                                                 effective: str, allowed: bool):
    identity = conversation(tmp_path, authority)
    result = execute(kind, tmp_path, effective, identity)
    if result.get("status") == "confirmation_required":
        assert not (tmp_path / "result.txt").exists()
        result = execute(kind, tmp_path, effective, identity, [result["approval_key"]])
    assert bool(result.get("success")) is allowed, result
    assert (tmp_path / "result.txt").exists() is allowed
    with connect() as db:
        assert db.execute("SELECT permission_mode FROM conversations WHERE id=?", (identity,)).fetchone()[0] == authority


@pytest.mark.parametrize("kind", ["batch", "extension"])
@pytest.mark.parametrize("change", ["mode", "delete", "workspace"])
def test_tightened_grant_is_revoked_when_authoritative_context_changes(tmp_path: Path, monkeypatch,
                                                                     kind: str, change: str):
    from app.tools import file_operations, runtime_tools
    identity = conversation(tmp_path, "full")
    pending = execute(kind, tmp_path, "ask", identity)
    module = file_operations if kind == "batch" else runtime_tools
    name = "execute_file_operation" if kind == "batch" else "execute_tool"
    original = getattr(module, name)

    def mutate_then_execute(*args, **kwargs):
        with connect() as db:
            if change == "mode":
                # Still stricter than agent, but not the authority captured
                # when this grant was issued: it must be invalidated.
                db.execute("UPDATE conversations SET permission_mode='agent' WHERE id=?", (identity,))
            elif change == "delete":
                db.execute("DELETE FROM conversations WHERE id=?", (identity,))
            else:
                db.execute("UPDATE conversations SET workspace=? WHERE id=?", (str(tmp_path.parent), identity))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, mutate_then_execute)
    result = execute(kind, tmp_path, "ask", identity, [pending["approval_key"]])
    assert result["success"] is False, result
    assert not (tmp_path / "result.txt").exists()
