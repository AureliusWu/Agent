from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable

from app.permissions import PermissionDecision, authorize
from app.sandbox import execute_tool


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
    batch_id = uuid.uuid4().hex
    results: list[dict[str, Any]] = []
    completed_change_ids: list[str] = []
    for index, request in enumerate(requests):
        result = execute_file_operation(
            workspace,
            request,
            mode=mode,
            dry_run=dry_run,
            approval_tokens=approval_tokens,
            approval_scope=approval_scope,
            conversation_id=conversation_id,
            task_id=task_id,
            tool_call_id=f"{batch_id}:{index}",
            permission_fn=permission_fn,
        )
        results.append(result)
        if result.get("change_id"):
            completed_change_ids.append(str(result["change_id"]))
        if not result.get("success"):
            rollback: list[dict[str, Any]] = []
            if not dry_run:
                for change_id in reversed(completed_change_ids):
                    rollback.append(
                        execute_tool(
                            workspace,
                            "full",
                            "undo_file_change",
                            {"change_id": change_id},
                            approval_scope="once",
                            conversation_id=conversation_id,
                            task_id=task_id,
                            tool_call_id=f"{batch_id}:rollback",
                            permission_fn=permission_fn,
                        )
                    )
            return {
                "success": False,
                "status": "error",
                "batch_id": batch_id,
                "failed_index": index,
                "results": results,
                "rolled_back": bool(completed_change_ids) and all(item.get("success") for item in rollback),
                "rollback": rollback,
                "error_code": "batch_operation_failed",
                "error_message": result.get("error_message") or "批量文件操作失败",
            }
    return {
        "success": True,
        "status": "ok",
        "batch_id": batch_id,
        "dry_run": dry_run,
        "operation_count": len(results),
        "results": results,
        "change_ids": completed_change_ids,
    }
