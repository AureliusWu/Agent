from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import connect
from app.main import app
from app.permissions import authorize, permission_denial, set_permission_policy, revoke_permission_policy
from app.sandbox import execute_tool, file_version_token
from app.tools.file_operations import FileOperationRequest, execute_file_batch


def operation(name: str, **arguments) -> FileOperationRequest:
    return FileOperationRequest(name, arguments)


def run_approved(workspace: Path, requests, mode: str, **kwargs):
    first = execute_file_batch(str(workspace), requests, mode=mode, **kwargs)
    if first.get("status") == "confirmation_required":
        return execute_file_batch(str(workspace), requests, mode=mode,
                                  approval_tokens=[first["approval_key"]], **kwargs)
    return first


@pytest.mark.parametrize("mode", ["ask", "agent", "full"])
@pytest.mark.parametrize("second", ["file.rename", "file.delete"])
def test_one_batch_approval_covers_sequential_created_file(tmp_path: Path, mode: str, second: str):
    args = {"expected_version_token": "batch:0"}
    if second == "file.rename":
        args.update(source="new.txt", destination="renamed.txt", expected_destination_version_token="missing")
    else:
        args["path"] = "new.txt"
    requests = [operation("file.write", path="new.txt", content="hello", expected_version_token="missing"),
                operation(second, **args)]
    result = run_approved(tmp_path, requests, mode)
    assert result["success"] is True, result
    assert not (tmp_path / "new.txt").exists()
    assert (tmp_path / "renamed.txt").exists() is (second == "file.rename")


def test_preview_is_metadata_only_and_never_makes_destination_parent(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_text("sensitive fixture content", encoding="utf-8")
    result = execute_file_batch(str(tmp_path), [operation("file.copy", source="source.txt",
        destination="nested/copied.txt", expected_version_token=file_version_token(source),
        expected_destination_version_token="missing")], mode="ask", dry_run=True)
    assert result["success"] is True, result
    assert not (tmp_path / "nested").exists()
    assert not (tmp_path / ".agent-backups").exists()
    assert "sensitive fixture content" not in json.dumps(result)


def test_sandbox_copy_dry_run_never_creates_parent(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    result = execute_tool(str(tmp_path), "full", "copy_file", {"source": "source.txt",
        "destination": "nested/copied.txt", "expected_version_token": file_version_token(source),
        "expected_destination_version_token": "missing", "dry_run": True})
    assert result["success"] is True
    assert not (tmp_path / "nested").exists()


@pytest.mark.parametrize("mode", ["ask", "agent", "full"])
def test_stale_preflight_does_not_write_first_file(tmp_path: Path, mode: str):
    (tmp_path / "existing.txt").write_text("new", encoding="utf-8")
    result = execute_file_batch(str(tmp_path), [
        operation("file.write", path="first.txt", content="one", expected_version_token="missing"),
        operation("file.write", path="existing.txt", content="two", expected_version_token="missing"),
    ], mode=mode)
    assert result["error_code"] == "batch_preflight_failed"
    assert result["failed_index"] == 1
    assert not (tmp_path / "first.txt").exists()


def test_readonly_cannot_use_batch_approval(tmp_path: Path):
    requests = [operation("file.write", path="no.txt", content="no", expected_version_token="missing")]
    pending = execute_file_batch(str(tmp_path), requests, mode="ask")
    denied = execute_file_batch(str(tmp_path), requests, mode="readonly",
                               approval_tokens=[pending["approval_key"]])
    assert denied["error_code"] == "read_only_mode"
    assert not (tmp_path / "no.txt").exists()


def test_child_delete_policy_still_denies_full_batch(tmp_path: Path):
    source = tmp_path / "keep.txt"
    source.write_text("keep", encoding="utf-8")
    policy = set_permission_policy(permission="filesystem.delete", effect="deny", scope="workspace",
                                   workspace=str(tmp_path), tool="delete_file")
    try:
        result = execute_file_batch(str(tmp_path), [operation("file.delete", path="keep.txt",
            expected_version_token=file_version_token(source))], mode="full")
        assert result["success"] is False
        assert source.exists()
    finally:
        revoke_permission_policy(policy["id"])


def test_batch_api_uses_executor_receipt_and_one_confirmation(tmp_path: Path):
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        payload = {"conversation_id": conversation["id"], "workspace": str(tmp_path),
                   "permission_mode": "ask", "tool": "file_batch", "arguments": {"operations": [
                       {"operation": "file.write", "arguments": {"path": "api.txt", "content": "api",
                                                                   "expected_version_token": "missing"}}]}}
        preview = client.post("/api/tools/execute", json={**payload,
            "arguments": {**payload["arguments"], "dry_run": True}}).json()
        assert preview["success"] is True, preview
        first = client.post("/api/tools/execute", json=payload).json()
        assert first["status"] == "confirmation_required", first
        executed = client.post("/api/tools/execute", json={**payload,
            "approval_tokens": [first["approval_key"]]}).json()
        assert executed["success"] is True, executed
        assert executed["receipt"]["success"] is True
        assert (tmp_path / "api.txt").read_text(encoding="utf-8") == "api"


@pytest.mark.parametrize("mode", ["ask", "agent", "full"])
def test_failed_second_step_rolls_back_under_original_mode(tmp_path: Path, mode: str, monkeypatch):
    from app.tools import file_operations
    original = file_operations.execute_file_operation
    original_tool = file_operations.execute_tool
    observed = []

    def injected(workspace, request, **kwargs):
        if request.arguments.get("path") == "second.txt":
            return {"success": False, "status": "error", "error_code": "injected_failure"}
        return original(workspace, request, **kwargs)

    def track_rollback(workspace, actual_mode, tool, arguments, *args, **kwargs):
        if tool == "undo_file_change":
            observed.append(actual_mode)
        return original_tool(workspace, actual_mode, tool, arguments, *args, **kwargs)

    monkeypatch.setattr(file_operations, "execute_file_operation", injected)
    monkeypatch.setattr(file_operations, "execute_tool", track_rollback)
    result = run_approved(tmp_path, [
        operation("file.write", path="first.txt", content="first", expected_version_token="missing"),
        operation("file.write", path="second.txt", content="second", expected_version_token="missing"),
    ], mode)
    assert result["success"] is False and result["rolled_back"] is True, result
    assert observed == [mode]
    assert not (tmp_path / "first.txt").exists()


def test_expired_batch_grant_stops_new_effects_but_rolls_back_its_own_effect(tmp_path: Path, monkeypatch):
    from app.tools import batch_grants, file_operations
    original = file_operations.execute_file_operation
    clock = [10.0]
    monkeypatch.setattr(batch_grants.time, "monotonic", lambda: clock[0])

    def advance(workspace, request, **kwargs):
        result = original(workspace, request, **kwargs)
        clock[0] += 121
        return result

    monkeypatch.setattr(file_operations, "execute_file_operation", advance)
    result = execute_file_batch(str(tmp_path), [
        operation("file.write", path="first.txt", content="first", expected_version_token="missing"),
        operation("file.write", path="second.txt", content="second", expected_version_token="missing"),
    ], mode="full")
    assert result["cause_error_code"] == "batch_grant_invalid", result
    assert result["rolled_back"] is True
    assert not (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()


def test_rollback_will_not_overwrite_an_external_edit(tmp_path: Path, monkeypatch):
    from app.tools import file_operations
    original = file_operations.execute_file_operation

    def injected(workspace, request, **kwargs):
        if request.arguments.get("path") == "second.txt":
            (tmp_path / "first.txt").write_text("external edit", encoding="utf-8")
            return {"success": False, "status": "error", "error_code": "injected_failure"}
        return original(workspace, request, **kwargs)

    monkeypatch.setattr(file_operations, "execute_file_operation", injected)
    result = execute_file_batch(str(tmp_path), [
        operation("file.write", path="first.txt", content="first", expected_version_token="missing"),
        operation("file.write", path="second.txt", content="second", expected_version_token="missing"),
    ], mode="full")
    assert result["rolled_back"] is False
    assert result["rollback"][0]["error_code"] == "batch_rollback_conflict"
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "external edit"


@pytest.mark.parametrize("changed", ["workspace", "conversation", "task", "operations"])
def test_approval_is_bound_to_exact_batch_context(tmp_path: Path, changed: str):
    requests = [operation("file.write", path="one.txt", content="one", expected_version_token="missing")]
    first = execute_file_batch(str(tmp_path), requests, mode="ask", task_id="task-one")
    workspace, conversation_id, task_id = tmp_path, None, "task-one"
    if changed == "workspace":
        workspace = tmp_path / "other"
        workspace.mkdir()
    if changed == "conversation":
        with TestClient(app) as client:
            conversation_id = client.post("/api/conversations", json={"workspace": str(tmp_path)}).json()["id"]
    if changed == "task":
        task_id = "task-two"
    if changed == "operations":
        requests = [operation("file.write", path="one.txt", content="changed", expected_version_token="missing")]
    result = execute_file_batch(str(workspace), requests, mode="ask", conversation_id=conversation_id,
        task_id=task_id, approval_tokens=[first["approval_key"]])
    assert result["status"] == "confirmation_required", result
    assert not (workspace / "one.txt").exists()


def test_user_denial_does_not_execute_any_child(tmp_path: Path):
    requests = [operation("file.write", path="denied.txt", content="denied", expected_version_token="missing")]
    first = execute_file_batch(str(tmp_path), requests, mode="ask")
    result = execute_file_batch(str(tmp_path), requests, mode="ask", approval_scope="deny",
        approval_tokens=[first["approval_key"]])
    try:
        assert result["error_code"] == "permission_denied", result
        assert not (tmp_path / "denied.txt").exists()
    finally:
        if result.get("policy_id"):
            revoke_permission_policy(result["policy_id"])


@pytest.mark.parametrize("mode", ["ask", "agent", "full", "readonly"])
def test_broker_and_batch_preconditions_share_mandatory_denials(tmp_path: Path, mode: str):
    values = {"mode": mode, "risk": "medium", "tool": "write_file", "workspace": str(tmp_path)}
    policy = set_permission_policy(permission="filesystem.write", effect="deny", scope="workspace", workspace=str(tmp_path))
    try:
        mandatory = permission_denial(**values)
        broker = authorize(**values, arguments={"path": "no.txt", "content": "no"})
        assert mandatory == broker
        assert mandatory.allowed is False
    finally:
        revoke_permission_policy(policy["id"])


def test_batch_reference_cannot_refer_to_different_path(tmp_path: Path):
    result = execute_file_batch(str(tmp_path), [
        operation("file.write", path="one.txt", content="first", expected_version_token="missing"),
        operation("file.delete", path="other.txt", expected_version_token="batch:0"),
    ], mode="full")
    assert result["error_code"] == "batch_preflight_failed"
    assert not (tmp_path / "one.txt").exists()


def test_batch_creates_only_one_approval_and_journal_never_duplicates_content(tmp_path: Path):
    private_content = "private fixture material that must not enter transaction journal"
    result = run_approved(tmp_path, [
        operation("file.write", path="one.txt", content=private_content, expected_version_token="missing"),
        operation("file.write", path="two.txt", content=private_content, expected_version_token="missing"),
    ], "ask")
    assert result["success"] is True
    with connect() as db:
        grants = db.execute("SELECT tool FROM approval_grants WHERE workspace=?", (str(tmp_path),)).fetchall()
        journal = db.execute("SELECT plan_json,result_json FROM file_transactions WHERE transaction_id=?",
                             (result["batch_id"],)).fetchone()
    assert [row["tool"] for row in grants] == ["file_batch"]
    assert private_content not in journal["plan_json"] + journal["result_json"]


def test_conversation_mode_change_revokes_batch_without_escalating_rollback(tmp_path: Path, monkeypatch):
    from app.tools import file_operations
    original = file_operations.execute_file_operation
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "full"}).json()

    def changed_mode(workspace, request, **kwargs):
        result = original(workspace, request, **kwargs)
        with connect() as db:
            db.execute("UPDATE conversations SET permission_mode='readonly' WHERE id=?", (conversation["id"],))
        return result

    monkeypatch.setattr(file_operations, "execute_file_operation", changed_mode)
    result = execute_file_batch(str(tmp_path), [
        operation("file.write", path="one.txt", content="one", expected_version_token="missing"),
        operation("file.write", path="two.txt", content="two", expected_version_token="missing"),
    ], mode="full", conversation_id=conversation["id"])
    assert result["cause_error_code"] == "batch_context_changed"
    assert result["rolled_back"] is False
    assert not (tmp_path / "two.txt").exists()


def test_unissued_or_serialized_batch_grants_are_not_accepted(tmp_path: Path):
    from app.tools.batch_grants import BatchGrant
    from app.tools.batch_plan import FileBatchPlan
    with pytest.raises(PermissionError):
        BatchGrant(object(), plan=FileBatchPlan(str(tmp_path)), operations_hash="forged",
                   mode="full", conversation_id=None, task_id=None, confirmed=True, batch_id="forged")


def test_external_edit_between_tool_and_grant_record_is_preserved(tmp_path: Path, monkeypatch):
    from app.tools import file_operations
    original = file_operations.execute_file_operation

    def interleaved(workspace, request, **kwargs):
        result = original(workspace, request, **kwargs)
        (tmp_path / "one.txt").write_text("external race", encoding="utf-8")
        return result

    monkeypatch.setattr(file_operations, "execute_file_operation", interleaved)
    result = execute_file_batch(str(tmp_path), [
        operation("file.write", path="one.txt", content="one", expected_version_token="missing"),
    ], mode="full")
    assert result["success"] is False
    assert result["rolled_back"] is False
    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "external race"


def test_created_directory_rollback_is_limited_to_empty_owned_directory(tmp_path: Path, monkeypatch):
    from app.tools import file_operations
    original = file_operations.execute_file_operation

    def injected(workspace, request, **kwargs):
        if request.arguments.get("path") == "fail.txt":
            return {"success": False, "status": "error", "error_code": "injected_failure"}
        return original(workspace, request, **kwargs)

    monkeypatch.setattr(file_operations, "execute_file_operation", injected)
    result = execute_file_batch(str(tmp_path), [operation("directory.create", path="created"),
        operation("file.write", path="created/one.txt", content="one", expected_version_token="missing"),
        operation("file.write", path="fail.txt", content="fail", expected_version_token="missing"),
    ], mode="full")
    assert result["rolled_back"] is True, result
    assert not (tmp_path / "created").exists()
