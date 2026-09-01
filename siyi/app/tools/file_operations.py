from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.database import connect, now_iso
from app.permissions import PermissionDecision, authorize, permission_denial
from app.sandbox import FileVersionError, SandboxError, execute_tool
from app.tools.batch_plan import BatchPlanError, FileBatchPlan
from app.tools.batch_grants import issue_batch_grant
from app.tools.registry import REGISTRY


CORE_FILE_OPERATIONS = {
    "file.read": "read_file",
    "file.write": "write_file",
    "file.patch": "apply_patch",
    "file.copy": "copy_file",
    "file.move": "move_file",
    "file.rename": "rename_file",
    "file.delete": "delete_file",
    "file.restore": "undo_file_change",
    "file.search": "search_files",
    "file.list": "list_files",
    "file.stat": "file_metadata",
    "directory.create": "create_directory",
    "directory.list": "list_directory",
    "directory.move": "move_file",
    "directory.delete": "delete_directory",
}
MUTATING_FILE_OPERATIONS = {
    "file.write",
    "file.patch",
    "file.copy",
    "file.move",
    "file.rename",
    "file.delete",
    "file.restore",
    "directory.create",
    "directory.move",
    "directory.delete",
}
MAX_BATCH_OPERATIONS = 50


@dataclass(frozen=True)
class FileOperationRequest:
    operation: str
    arguments: dict[str, Any]


def _task_reference(task_id: str | None) -> str | None:
    if not task_id:
        return None
    with connect() as db:
        return task_id if db.execute("SELECT 1 FROM agent_tasks WHERE id=?", (task_id,)).fetchone() else None


def _safe_plan(requests: list[FileOperationRequest]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for request in requests:
        arguments = request.arguments
        safe = {
            key: arguments[key]
            for key in ("path", "source", "destination", "expected_version_token", "expected_destination_version_token")
            if key in arguments
        }
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        plan.append(
            {
                "operation": request.operation,
                "arguments": safe,
                "arguments_sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )
    return plan


def _start_transaction(
    transaction_id: str,
    workspace: str,
    requests: list[FileOperationRequest],
    task_id: str | None,
) -> None:
    stamp = now_iso()
    workspace_hash = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()
    with connect() as db:
        db.execute(
            "INSERT INTO file_transactions(transaction_id,task_id,workspace_hash,status,operation_count,plan_json,result_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?, ?,?)",
            (
                transaction_id,
                _task_reference(task_id),
                workspace_hash,
                "preflight",
                len(requests),
                json.dumps(_safe_plan(requests), ensure_ascii=False, sort_keys=True),
                "{}",
                stamp,
                stamp,
            ),
        )


def _finish_transaction(transaction_id: str, status: str, result: dict[str, Any]) -> None:
    with connect() as db:
        db.execute(
            "UPDATE file_transactions SET status=?,result_json=?,updated_at=? WHERE transaction_id=?",
            (status, json.dumps(_transaction_metadata(result), ensure_ascii=False, sort_keys=True, default=str)[:100_000], now_iso(), transaction_id),
        )


def _record_rollback(transaction_id: str, task_id: str | None, change_id: str, result: dict[str, Any]) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO rollback_records(transaction_id,task_id,change_id,status,result_json,created_at) VALUES(?,?,?,?,?,?)",
            (
                transaction_id,
                _task_reference(task_id),
                change_id,
                "complete" if result.get("success") else "failed",
                json.dumps(_transaction_metadata(result), ensure_ascii=False, sort_keys=True, default=str)[:40_000],
                now_iso(),
            ),
        )


def _transaction_metadata(result: dict[str, Any]) -> dict[str, Any]:
    """The operation backup owns bytes; transaction journals contain metadata only."""
    fields = {
        "success", "status", "batch_id", "dry_run", "operation_count", "failed_index",
        "rolled_back", "error_code", "cause_error_code", "retryable", "operation",
        "adapter_tool", "change_id", "change_ids", "path", "paths", "source", "destination",
        "restored", "version_before", "version_after", "confirmed", "preflight", "results", "rollback",
    }
    return {key: ([_transaction_metadata(item) if isinstance(item, dict) else item for item in value]
                  if isinstance(value, list) else value)
            for key, value in result.items() if key in fields}


def execute_file_operation(
    workspace: str,
    request: FileOperationRequest,
    *,
    mode: str = "full",
    dry_run: bool = False,
    approval_tokens: list[str] | None = None,
    approval_scope: str = "once",
    conversation_id: int | None = None,
    task_id: str | None = None,
    tool_call_id: str | None = None,
    permission_fn: Callable[..., PermissionDecision] = authorize,
) -> dict[str, Any]:
    legacy_name = CORE_FILE_OPERATIONS.get(request.operation)
    if legacy_name is None:
        return {
            "success": False,
            "status": "error",
            "operation": request.operation,
            "error_code": "unknown_file_operation",
            "error_message": f"未知核心文件操作：{request.operation}",
            "retryable": False,
        }
    arguments = dict(request.arguments)
    dry_run = bool(arguments.pop("dry_run", dry_run))
    if dry_run and request.operation in MUTATING_FILE_OPERATIONS:
        if request.operation == "file.restore":
            changes = execute_tool(
                workspace,
                mode,
                "list_file_changes",
                {"task_id": str(arguments["task_id"])} if arguments.get("task_id") else {},
                approval_tokens,
                approval_scope=approval_scope,
                conversation_id=conversation_id,
                task_id=task_id,
                tool_call_id=tool_call_id,
                permission_fn=permission_fn,
            )
            return {
                **changes,
                "dry_run": True,
                "operation": request.operation,
                "would_restore": changes.get("changes") or [],
            }
        arguments["dry_run"] = True
    result = execute_tool(
        workspace,
        mode,
        legacy_name,
        arguments,
        approval_tokens,
        approval_scope=approval_scope,
        conversation_id=conversation_id,
        task_id=task_id,
        tool_call_id=tool_call_id,
        permission_fn=permission_fn,
    )
    return {
        **result,
        "operation": request.operation,
        "adapter_tool": legacy_name,
        "dry_run": bool(result.get("dry_run", False)),
    }


def execute_file_batch(
    workspace: str,
    requests: list[FileOperationRequest],
    *,
    mode: str = "full",
    dry_run: bool = False,
    approval_tokens: list[str] | None = None,
    approval_scope: str = "once",
    conversation_id: int | None = None,
    task_id: str | None = None,
    permission_fn: Callable[..., PermissionDecision] = authorize,
) -> dict[str, Any]:
    if not requests:
        return {
            "success": False,
            "status": "error",
            "error_code": "empty_batch",
            "error_message": "批量文件操作不能为空",
        }
    if len(requests) > MAX_BATCH_OPERATIONS:
        return {
            "success": False,
            "status": "error",
            "error_code": "batch_limit_exceeded",
            "error_message": f"单批最多允许 {MAX_BATCH_OPERATIONS} 项文件操作",
        }
    mode = {"confirm": "ask", "auto": "full"}.get(mode, mode)
    denied = permission_denial(mode=mode, risk=REGISTRY["file_batch"].risk,
                               tool="file_batch", workspace=workspace)
    if denied:
        return denied.confirmation
    # Copy untrusted request data once. Neither a caller nor a callback can
    # mutate the approved plan while the transaction is being executed.
    requests = [FileOperationRequest(item.operation, json.loads(json.dumps(item.arguments))) for item in requests]
    batch_id = uuid.uuid4().hex
    plan = FileBatchPlan(workspace)
    try:
        for index, request in enumerate(requests):
            tool = CORE_FILE_OPERATIONS.get(request.operation)
            if tool is None:
                raise BatchPlanError(index, "unknown_file_operation", f"未知核心文件操作：{request.operation}")
            plan.append(request.operation, tool, request.arguments, mode)
        plan.verify_initial()
    except (SandboxError, BatchPlanError, OSError, ValueError, KeyError) as exc:
        failure = {
            "success": False, "status": "error", "batch_id": batch_id,
            "failed_index": getattr(exc, "index", len(plan.steps)), "preflight": plan.preview(),
            "results": [], "rolled_back": True, "rollback": [],
            "error_code": "batch_preflight_failed", "cause_error_code": getattr(exc, "code", "invalid_arguments"),
            "error_message": str(exc),
        }
        if not dry_run:
            _start_transaction(batch_id, workspace, requests, task_id)
            _finish_transaction(batch_id, "preflight_failed", failure)
        return failure
    preflight = plan.preview()
    if dry_run:
        return {"success": True, "status": "ok", "dry_run": True, "operation_count": len(requests),
                "preflight": preflight, "results": preflight, "plan": _safe_plan(requests)}
    approval_arguments = {"operations": [{"operation": item.operation, "arguments": item.arguments}
                                          for item in requests], "dry_run": False}
    decision = permission_fn(mode=mode, risk=REGISTRY["file_batch"].risk, tool="file_batch",
        arguments=approval_arguments, conversation_id=conversation_id, task_id=task_id,
        approval_tokens=approval_tokens, approval_scope=approval_scope,
        impact=f"当前工作区 {len(requests)} 项文件操作及仅本批变更的失败回滚", workspace=workspace)
    if not decision.allowed:
        return {**(decision.confirmation or {"success": False, "status": "confirmation_required"}),
                "preflight": preflight}
    operations_hash = hashlib.sha256(json.dumps(approval_arguments, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    grant = issue_batch_grant(decision=decision, plan=plan, operations_hash=operations_hash, mode=mode,
        conversation_id=conversation_id, task_id=task_id, batch_id=batch_id)
    _start_transaction(batch_id, workspace, requests, task_id)
    results: list[dict[str, Any]] = []
    completed_change_ids: list[str] = []
    try:
        for index, step in enumerate(plan.steps):
            try:
                plan.verify_step(index)
                result = execute_file_operation(
                    workspace, FileOperationRequest(step.operation, step.arguments), mode=mode,
                    conversation_id=conversation_id, task_id=task_id, tool_call_id=f"{batch_id}:{index}",
                    permission_fn=grant.child_permission(index, operations_hash),
                )
                if result.get("change_id"):
                    completed_change_ids.append(str(result["change_id"]))
                    grant.record_change(str(result["change_id"]), index)
            except (SandboxError, OSError, ValueError, PermissionError) as exc:
                result = {"success": False, "status": "error", "error_code": getattr(exc, "code", "batch_execution_error"),
                          "error_message": str(exc)}
            results.append(result)
            if not result.get("success"):
                rollback: list[dict[str, Any]] = []
                for change_id in reversed(completed_change_ids):
                    rollback.append(
                        execute_tool(
                            workspace,
                            mode,
                            "undo_file_change",
                            {"change_id": change_id},
                            approval_scope="once",
                            conversation_id=conversation_id,
                            task_id=task_id,
                            tool_call_id=f"{batch_id}:rollback",
                            permission_fn=grant.rollback_permission(change_id),
                        )
                    )
                    _record_rollback(batch_id, task_id, change_id, rollback[-1])
                failure = {
                    "success": False, "status": "error", "batch_id": batch_id, "failed_index": index,
                    "results": results, "rolled_back": all(item.get("success") for item in rollback),
                    "rollback": rollback, "error_code": "batch_operation_failed",
                    "cause_error_code": result.get("error_code"),
                    "error_message": result.get("error_message") or "批量文件操作失败",
                }
                _finish_transaction(batch_id, "rolled_back" if failure["rolled_back"] else "rollback_failed", failure)
                return failure
        success = {"success": True, "status": "ok", "batch_id": batch_id, "dry_run": False,
                   "operation_count": len(results), "results": results, "change_ids": completed_change_ids,
                   "preflight": preflight, "confirmed": decision.confirmed,
                   "paths": sorted({path for step in plan.steps for path in step.after})}
        _finish_transaction(batch_id, "committed", success)
        return success
    finally:
        grant.close()
